"""Decorator and context manager that record spans for functions and blocks of code."""

from __future__ import annotations

import functools
import inspect
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from types import TracebackType
from typing import Literal, ParamSpec, TypeVar, cast, get_args, overload

P = ParamSpec("P")
R = TypeVar("R")
T = TypeVar("T")

SpanKind = Literal["run", "step", "llm", "tool"]
_SPAN_KINDS: tuple[SpanKind, ...] = get_args(SpanKind)

AttributeValue = str | int | float | bool | None
"""A span attribute is a primitive value, so it can be stored and exported as-is."""

_current: ContextVar[Span | None] = ContextVar("yolotrace_current_span", default=None)
_finished: list[Span] = []


@dataclass
class Span:
    """One recorded unit of work, linked to its parent through ``parent_id``."""

    name: str
    span_id: str
    parent_id: str | None
    start_time: float
    end_time: float | None = None
    status: str = "ok"
    error: str | None = None
    kind: SpanKind = "step"
    attributes: dict[str, AttributeValue] = field(default_factory=dict)

    @property
    def duration(self) -> float | None:
        """Seconds between start and end, or ``None`` while the span is still running."""
        if self.end_time is None:
            return None
        return self.end_time - self.start_time

    def set_attribute(self, key: str, value: AttributeValue) -> None:
        """Store ``value`` under ``key``, replacing any earlier value for that key."""
        self.attributes[key] = value

    def end(self, exc: BaseException | None = None) -> None:
        """Finish a span started with ``start_span``, optionally marking it as failed."""
        if self.end_time is not None:
            raise RuntimeError(f"span {self.name!r} has already ended")
        self.end_time = time.time()
        if exc is not None:
            self.status = "error"
            self.error = f"{type(exc).__name__}: {exc}"
        _finished.append(self)


def current_span() -> Span | None:
    """Return the span whose block or function is running in this context, if any."""
    return _current.get()


def finished_spans() -> list[Span]:
    """Return a copy of every span that has ended in this process, in end order."""
    return list(_finished)


def start_span(
    name: str,
    *,
    kind: SpanKind = "step",
    attributes: Mapping[str, AttributeValue] | None = None,
) -> Span:
    """Start a span that is ended explicitly with ``span.end()``.

    Use it when the work does not fit one ``with`` block or function, for example
    when a span starts in one callback and ends in another. The span is parented to
    the current span but does not become the current span, so nested ``trace``
    blocks do not link to it.
    """
    if kind not in _SPAN_KINDS:
        raise ValueError(f"unknown span kind: {kind!r}")
    return _new_span(name, kind, dict(attributes or {}))


def _new_span(name: str, kind: SpanKind, attributes: dict[str, AttributeValue]) -> Span:
    parent = _current.get()
    return Span(
        name=name,
        span_id=uuid.uuid4().hex,
        parent_id=parent.span_id if parent else None,
        start_time=time.time(),
        kind=kind,
        attributes=attributes,
    )


def _iterate_sync[T](
    source: Generator[T, None, None],
    name: str,
    kind: SpanKind,
    attributes: dict[str, AttributeValue],
) -> Generator[T, None, None]:
    """Yield the items of ``source`` inside a span that ends when the generator finishes.

    The span becomes current only while ``source`` runs, so the caller's context
    never sees it between two yielded items.
    """
    span = _new_span(name, kind, attributes)
    while True:
        token = _current.set(span)
        try:
            item = next(source)
        except StopIteration:
            span.end()
            return
        except BaseException as exc:
            span.end(exc)
            raise
        finally:
            _current.reset(token)
        try:
            yield item
        except GeneratorExit:
            source.close()
            span.end()
            raise


async def _iterate_async[T](
    source: AsyncGenerator[T, None],
    name: str,
    kind: SpanKind,
    attributes: dict[str, AttributeValue],
) -> AsyncGenerator[T, None]:
    """Async counterpart of ``_iterate_sync`` for async generators."""
    span = _new_span(name, kind, attributes)
    while True:
        token = _current.set(span)
        try:
            item = await source.__anext__()
        except StopAsyncIteration:
            span.end()
            return
        except BaseException as exc:
            span.end(exc)
            raise
        finally:
            _current.reset(token)
        try:
            yield item
        except GeneratorExit:
            await source.aclose()
            span.end()
            raise


def _function_name(func: object) -> str:
    qualname = getattr(func, "__qualname__", None)
    if not isinstance(qualname, str):
        raise TypeError(f"cannot trace {func!r}: it has no qualified name")
    return qualname


class trace:
    """Record a span around a function call, a block, or a stream of values.

    Works as ``@trace()`` on sync or ``async def`` functions, as ``@trace()`` on
    generator and async generator functions, and as ``with trace("name")`` or
    ``async with trace("name")`` blocks. A generator's span starts at its first
    item and ends when the generator is exhausted, fails, or is closed early.

    ``kind`` is one of "run", "step", "llm" or "tool". ``attributes`` holds metadata
    known before the call; use ``current_span().set_attribute`` for values known after.
    """

    def __init__(
        self,
        name: str | None = None,
        *,
        kind: SpanKind = "step",
        attributes: Mapping[str, AttributeValue] | None = None,
    ) -> None:
        if kind not in _SPAN_KINDS:
            raise ValueError(f"unknown span kind: {kind!r}")
        self._name = name
        self._kind = kind
        self._attributes: dict[str, AttributeValue] = dict(attributes or {})
        self._span: Span | None = None
        self._token: Token[Span | None] | None = None

    @overload
    def __call__(
        self, func: Callable[P, AsyncGenerator[T, None]]
    ) -> Callable[P, AsyncGenerator[T, None]]: ...

    @overload
    def __call__(
        self, func: Callable[P, Generator[T, None, None]]
    ) -> Callable[P, Generator[T, None, None]]: ...

    @overload
    def __call__(
        self, func: Callable[P, Awaitable[R]]
    ) -> Callable[P, Awaitable[R]]: ...

    @overload
    def __call__(self, func: Callable[P, R]) -> Callable[P, R]: ...

    def __call__(self, func: Callable[P, object]) -> Callable[P, object]:
        name = self._name or _function_name(func)
        kind = self._kind
        attributes = self._attributes

        if inspect.isasyncgenfunction(func):
            agen_func = cast(Callable[P, AsyncGenerator[object, None]], func)

            @functools.wraps(func)
            def async_gen_wrapper(
                *args: P.args, **kwargs: P.kwargs
            ) -> AsyncGenerator[object, None]:
                return _iterate_async(
                    agen_func(*args, **kwargs), name, kind, dict(attributes)
                )

            return async_gen_wrapper

        if inspect.isgeneratorfunction(func):
            gen_func = cast(Callable[P, Generator[object, None, None]], func)

            @functools.wraps(func)
            def gen_wrapper(
                *args: P.args, **kwargs: P.kwargs
            ) -> Generator[object, None, None]:
                return _iterate_sync(
                    gen_func(*args, **kwargs), name, kind, dict(attributes)
                )

            return gen_wrapper

        if inspect.iscoroutinefunction(func):
            awaitable_func = cast(Callable[P, Awaitable[object]], func)

            @functools.wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> object:
                with trace(name, kind=kind, attributes=attributes):
                    return await awaitable_func(*args, **kwargs)

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> object:
            with trace(name, kind=kind, attributes=attributes):
                return func(*args, **kwargs)

        return wrapper

    def __enter__(self) -> Span:
        if self._name is None:
            raise ValueError(
                "a span name is required when using trace as a context manager"
            )
        self._span = _new_span(self._name, self._kind, dict(self._attributes))
        self._token = _current.set(self._span)
        return self._span

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        span = self._span
        token = self._token
        assert span is not None and token is not None
        _current.reset(token)
        span.end(exc)

    async def __aenter__(self) -> Span:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc, tb)
