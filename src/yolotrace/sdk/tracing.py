"""Decorator and context manager that record spans for functions and blocks of code."""

from __future__ import annotations

import contextvars
import functools
import inspect
import random
import re
import secrets
import time
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Generator,
    Iterator,
    Mapping,
    MutableMapping,
)
from contextlib import contextmanager
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

TRACEPARENT_KEY = "traceparent"
"""Carrier key under which ``inject`` writes and ``extract`` reads the trace context."""

_TRACEPARENT_PATTERN = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})")
_SAMPLED_FLAG = 0x01


@dataclass(frozen=True, slots=True)
class _Settings:
    """Recording settings that ``configure`` sets. The defaults record every trace in full."""

    sample_rate: float = 1.0
    max_attributes: int = 128
    max_attribute_length: int = 4096


_settings = _Settings()
_current: ContextVar[Span | None] = ContextVar("yolotrace_current_span", default=None)
_remote: ContextVar[SpanContext | None] = ContextVar(
    "yolotrace_remote_context", default=None
)
_finished: list[Span] = []
_running: dict[str, Span] = {}


@dataclass(frozen=True)
class SpanContext:
    """The trace and span IDs that identify a point in a trace, possibly in another process.

    ``sampled`` says whether the trace is recorded. Spans under an unsampled context are
    not recorded either.
    """

    trace_id: str
    span_id: str
    sampled: bool = True


@dataclass
class Span:
    """One recorded unit of work, linked to its parent through ``parent_id``.

    ``trace_id`` is shared by every span of one trace, including spans recorded in
    other processes that continued the trace through ``inject`` and ``extract``.
    """

    name: str
    trace_id: str
    span_id: str
    parent_id: str | None
    start_time: float
    end_time: float | None = None
    status: str = "ok"
    error: str | None = None
    kind: SpanKind = "step"
    attributes: dict[str, AttributeValue] = field(default_factory=dict)
    sampled: bool = True

    @property
    def duration(self) -> float | None:
        """Seconds between start and end, or ``None`` while the span is still running."""
        if self.end_time is None:
            return None
        return self.end_time - self.start_time

    def set_attribute(self, key: str, value: AttributeValue) -> None:
        """Store ``value`` under ``key``, replacing any earlier value for that key.

        A new key is dropped once the span holds ``max_attributes`` keys, and a string
        longer than ``max_attribute_length`` is cut short, so no error is raised.
        """
        if (
            key not in self.attributes
            and len(self.attributes) >= _settings.max_attributes
        ):
            return
        self.attributes[key] = _limit(value)

    def end(self, exc: BaseException | None = None) -> None:
        """Finish a span started with ``start_span``, optionally marking it as failed."""
        if self.end_time is not None:
            raise RuntimeError(f"span {self.name!r} has already ended")
        self.end_time = time.time()
        if exc is not None:
            self.status = "error"
            self.error = _truncate(f"{type(exc).__name__}: {exc}")
        _running.pop(self.span_id, None)
        if self.sampled:
            _finished.append(self)


def configure(
    *,
    sample_rate: float = 1.0,
    max_attributes: int = 128,
    max_attribute_length: int = 4096,
) -> None:
    """Set how much the SDK records. Each call replaces every setting; omitted ones reset to their defaults.

    ``sample_rate`` is the fraction of new traces to record, from 0.0 to 1.0. The choice
    is made once, where a trace starts, and every span of that trace follows it, including
    spans in other processes that continued the trace through ``extract``. Spans that are
    not sampled still run, so their code keeps working, but nothing is stored for them.

    ``max_attributes`` caps the number of attributes a span keeps. ``max_attribute_length``
    caps the length of string attributes and of error messages. Values over a limit are
    dropped or cut short; they never raise an error.
    """
    if not 0.0 <= sample_rate <= 1.0:
        raise ValueError(f"sample_rate must be between 0.0 and 1.0: {sample_rate!r}")
    if max_attributes < 0 or max_attribute_length < 0:
        raise ValueError("max_attributes and max_attribute_length cannot be negative")
    global _settings
    _settings = _Settings(
        sample_rate=sample_rate,
        max_attributes=max_attributes,
        max_attribute_length=max_attribute_length,
    )


def _limit(value: AttributeValue) -> AttributeValue:
    """Return ``value`` cut to the configured length when it is a string."""
    if isinstance(value, str):
        return _truncate(value)
    return value


def _truncate(text: str) -> str:
    """Return ``text`` cut to the configured maximum attribute length."""
    return text[: _settings.max_attribute_length]


def current_span() -> Span | None:
    """Return the span whose block or function is running in this context, if any."""
    return _current.get()


def inject(carrier: MutableMapping[str, str]) -> None:
    """Write the active trace context into ``carrier`` so another process can continue it.

    Call it where work leaves this process, such as before an HTTP request or when a
    job is queued, and pass the carrier along with the work. Nothing is written when
    no span or remote context is active.
    """
    context = _active_context()
    if context is not None:
        flags = "01" if context.sampled else "00"
        carrier[TRACEPARENT_KEY] = f"00-{context.trace_id}-{context.span_id}-{flags}"


def extract(carrier: Mapping[str, str]) -> SpanContext | None:
    """Read the trace context that ``inject`` wrote into ``carrier``.

    Returns ``None`` when the value is missing or malformed. Carriers often come from
    untrusted callers, so a bad value starts a new trace instead of raising an error.
    The key is ``traceparent`` in lower case, so normalize HTTP header names first.
    """
    value = carrier.get(TRACEPARENT_KEY)
    match = _TRACEPARENT_PATTERN.fullmatch(value) if value else None
    if match is None:
        return None
    trace_id, span_id, flags = match.groups()
    if set(trace_id) == {"0"} or set(span_id) == {"0"}:
        return None
    sampled = bool(int(flags, 16) & _SAMPLED_FLAG)
    return SpanContext(trace_id=trace_id, span_id=span_id, sampled=sampled)


@contextmanager
def continue_trace(context: SpanContext | None) -> Iterator[None]:
    """Make root spans started in this block children of ``context``, a span from elsewhere.

    The new spans share ``context.trace_id`` and link to ``context.span_id``, even though
    that span is recorded in another process. Use it with the result of ``extract``.
    A local span that is already running still takes precedence over ``context``.
    """
    token = _remote.set(context)
    try:
        yield
    finally:
        _remote.reset(token)


def _active_context() -> SpanContext | None:
    """Return the context a new span or an injected carrier should attach to."""
    span = _current.get()
    if span is not None:
        return SpanContext(
            trace_id=span.trace_id, span_id=span.span_id, sampled=span.sampled
        )
    return _remote.get()


def finished_spans() -> list[Span]:
    """Return a copy of every span that has ended in this process, in end order."""
    return list(_finished)


def running_spans() -> list[Span]:
    """Return every span that has started but not ended yet, in start order.

    Use it to see long-running workflows and background jobs while they are still
    working, before their spans appear in ``finished_spans``.
    """
    return list(_running.values())


def propagate_context[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Return ``func`` wrapped so it runs in a copy of the current context.

    Threads and executors do not copy context variables, so work handed to them
    would lose its parent span. Call this in the thread that starts the work, then
    give the result to the thread or executor. Spans that ``func`` records are
    parented to the span current at the time of this call.
    """
    context = contextvars.copy_context()

    @functools.wraps(func)
    def run_in_context(*args: P.args, **kwargs: P.kwargs) -> R:
        return context.run(func, *args, **kwargs)

    return run_in_context


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
    context = _active_context()
    # Sampling is load shedding, not a security decision, so a fast PRNG is enough.
    sampled = context.sampled if context else random.random() < _settings.sample_rate  # nosec B311
    span = Span(
        name=name,
        trace_id=context.trace_id if context else secrets.token_hex(16),
        span_id=secrets.token_hex(8),
        parent_id=context.span_id if context else None,
        start_time=time.time(),
        kind=kind,
        sampled=sampled,
    )
    for key, value in attributes.items():
        span.set_attribute(key, value)
    if sampled:
        _running[span.span_id] = span
    return span


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
