"""Decorator and context manager that record spans for functions and blocks of code."""

from __future__ import annotations

import functools
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Literal, TypeVar, get_args

F = TypeVar("F", bound=Callable[..., Any])

SpanKind = Literal["run", "step", "llm", "tool"]
_SPAN_KINDS = get_args(SpanKind)

_current: ContextVar[Span | None] = ContextVar("yolotrace_current_span", default=None)
_finished: list[Span] = []


@dataclass
class Span:
    name: str
    span_id: str
    parent_id: str | None
    start_time: float
    end_time: float | None = None
    status: str = "ok"
    error: str | None = None
    kind: SpanKind = "step"
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float | None:
        if self.end_time is None:
            return None
        return self.end_time - self.start_time

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


def current_span() -> Span | None:
    return _current.get()


def finished_spans() -> list[Span]:
    return list(_finished)


class trace:
    """Record a span around a function call (``@trace()``) or a block (``with trace("name")``).

    ``kind`` is one of "run", "step", "llm" or "tool". ``attributes`` holds metadata
    known before the call; use ``current_span().set_attribute`` for values known after.
    """

    def __init__(
        self,
        name: str | None = None,
        *,
        kind: SpanKind = "step",
        attributes: dict[str, Any] | None = None,
    ) -> None:
        if kind not in _SPAN_KINDS:
            raise ValueError(f"unknown span kind: {kind!r}")
        self._name = name
        self._kind = kind
        self._attributes = dict(attributes or {})
        self._span: Span | None = None
        self._token: Any = None

    def __call__(self, func: F) -> F:
        name = self._name or func.__qualname__

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with trace(name, kind=self._kind, attributes=self._attributes):
                return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    def __enter__(self) -> Span:
        if self._name is None:
            raise ValueError(
                "a span name is required when using trace as a context manager"
            )
        parent = _current.get()
        self._span = Span(
            name=self._name,
            span_id=uuid.uuid4().hex,
            parent_id=parent.span_id if parent else None,
            start_time=time.time(),
            kind=self._kind,
            attributes=dict(self._attributes),
        )
        self._token = _current.set(self._span)
        return self._span

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        span = self._span
        assert span is not None
        span.end_time = time.time()
        if exc is not None:
            span.status = "error"
            span.error = f"{type(exc).__name__}: {exc}"
        _current.reset(self._token)
        _finished.append(span)
        return False
