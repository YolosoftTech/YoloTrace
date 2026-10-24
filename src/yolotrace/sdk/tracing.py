"""Decorator and context manager that record spans for functions and blocks of code."""

from __future__ import annotations

import functools
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from types import TracebackType
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

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

    @property
    def duration(self) -> float | None:
        if self.end_time is None:
            return None
        return self.end_time - self.start_time


def finished_spans() -> list[Span]:
    return list(_finished)


class trace:
    """Record a span around a function call (``@trace()``) or a block (``with trace("name")``)."""

    def __init__(self, name: str | None = None) -> None:
        self._name = name
        self._span: Span | None = None
        self._token: Any = None

    def __call__(self, func: F) -> F:
        name = self._name or func.__qualname__

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with trace(name):
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
