"""Wrappers that record provider LLM calls as spans, shared by every provider SDK.

This module does not import any provider package. Each provider module supplies a
``Provider`` that says how to read its token counts, so the wrappers work the same way
for OpenAI, Anthropic and any future client.
"""

from __future__ import annotations

import functools
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Protocol, Self, cast

from yolotrace.sdk.tracing import AttributeValue, Span, start_span, trace

_MARKER = "__yolotrace_instrumented__"

TokenCounts = tuple[int | None, int | None]
"""Input and output token counts of one response or stream chunk; ``None`` when unreported."""


class SyncStream(Protocol):
    """A provider stream that yields chunks and can be closed before it is exhausted."""

    def __iter__(self) -> Iterator[object]: ...

    def close(self) -> None: ...


class AsyncStream(Protocol):
    """An async provider stream that yields chunks and can be closed before it is exhausted."""

    def __aiter__(self) -> AsyncIterator[object]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class Provider:
    """How to record the calls of one provider SDK."""

    name: str
    span_name: str
    read_tokens: Callable[[object], TokenCounts]


def is_wrapped(func: object) -> bool:
    """Return True when ``func`` already records spans, so it must not be wrapped again."""
    return getattr(func, _MARKER, False) is True


class SyncCreate(Protocol):
    """A sync provider ``create`` method as the wrapper sees it: arguments pass through unchanged."""

    def __call__(self, *args: object, **kwargs: object) -> object: ...


class AsyncCreate(Protocol):
    """An async provider ``create`` method as the wrapper sees it: arguments pass through unchanged."""

    def __call__(self, *args: object, **kwargs: object) -> Awaitable[object]: ...


def wrap_create(original: SyncCreate, provider: Provider) -> SyncCreate:
    """Wrap a sync ``create`` method so each call becomes an "llm" span.

    A call with ``stream=True`` returns a proxy whose span ends when the stream is consumed.
    """

    @functools.wraps(original)
    def create(*args: object, **kwargs: object) -> object:
        attributes = _request_attributes(provider, kwargs)
        if kwargs.get("stream"):
            span = start_span(provider.span_name, kind="llm", attributes=attributes)
            try:
                # With stream=True the provider SDK returns its stream object.
                stream = cast(SyncStream, original(*args, **kwargs))
            except BaseException as exc:
                span.end(exc)
                raise
            return TracedStream(stream, span, provider.read_tokens)
        with trace(provider.span_name, kind="llm", attributes=attributes) as span:
            response = original(*args, **kwargs)
            _record_tokens(span, provider.read_tokens(response))
            return response

    setattr(create, _MARKER, True)
    return create


def wrap_create_async(original: AsyncCreate, provider: Provider) -> AsyncCreate:
    """Wrap an async ``create`` method so each call becomes an "llm" span.

    A call with ``stream=True`` returns a proxy whose span ends when the stream is consumed.
    """

    @functools.wraps(original)
    async def create(*args: object, **kwargs: object) -> object:
        attributes = _request_attributes(provider, kwargs)
        if kwargs.get("stream"):
            span = start_span(provider.span_name, kind="llm", attributes=attributes)
            try:
                # With stream=True the provider SDK returns its stream object.
                stream = cast(AsyncStream, await original(*args, **kwargs))
            except BaseException as exc:
                span.end(exc)
                raise
            return TracedAsyncStream(stream, span, provider.read_tokens)
        with trace(provider.span_name, kind="llm", attributes=attributes) as span:
            response = await original(*args, **kwargs)
            _record_tokens(span, provider.read_tokens(response))
            return response

    setattr(create, _MARKER, True)
    return create


def _request_attributes(
    provider: Provider, kwargs: Mapping[str, object]
) -> dict[str, AttributeValue]:
    model = kwargs.get("model")
    return {
        "provider": provider.name,
        "model": model if isinstance(model, str) else None,
    }


def _record_tokens(span: Span, counts: TokenCounts) -> None:
    """Copy token counts onto ``span``. A ``None`` count leaves any earlier value in place."""
    input_tokens, output_tokens = counts
    if input_tokens is not None:
        span.set_attribute("input_tokens", input_tokens)
    if output_tokens is not None:
        span.set_attribute("output_tokens", output_tokens)


class TracedStream:
    """Proxy for a sync provider stream that ends its span once the stream is consumed.

    Each chunk's token counts overwrite earlier ones, so the last reported counts win.
    Attributes the proxy does not define are read from the provider stream.
    """

    def __init__(
        self,
        stream: SyncStream,
        span: Span,
        read_tokens: Callable[[object], TokenCounts],
    ) -> None:
        self._stream = stream
        self._chunks = iter(stream)
        self._span = span
        self._read_tokens = read_tokens

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> object:
        try:
            chunk = next(self._chunks)
        except StopIteration:
            self._finish()
            raise
        except BaseException as exc:
            self._finish(exc)
            raise
        _record_tokens(self._span, self._read_tokens(chunk))
        return chunk

    def close(self) -> None:
        """Close the provider stream and end the span, if it has not ended yet."""
        self._stream.close()
        self._finish()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)

    def _finish(self, exc: BaseException | None = None) -> None:
        if self._span.end_time is None:
            self._span.end(exc)


class TracedAsyncStream:
    """Proxy for an async provider stream that ends its span once the stream is consumed.

    Each chunk's token counts overwrite earlier ones, so the last reported counts win.
    Attributes the proxy does not define are read from the provider stream.
    """

    def __init__(
        self,
        stream: AsyncStream,
        span: Span,
        read_tokens: Callable[[object], TokenCounts],
    ) -> None:
        self._stream = stream
        self._chunks = stream.__aiter__()
        self._span = span
        self._read_tokens = read_tokens

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> object:
        try:
            chunk = await self._chunks.__anext__()
        except StopAsyncIteration:
            self._finish()
            raise
        except BaseException as exc:
            self._finish(exc)
            raise
        _record_tokens(self._span, self._read_tokens(chunk))
        return chunk

    async def close(self) -> None:
        """Close the provider stream and end the span, if it has not ended yet."""
        await self._stream.close()
        self._finish()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)

    def _finish(self, exc: BaseException | None = None) -> None:
        if self._span.end_time is None:
            self._span.end(exc)
