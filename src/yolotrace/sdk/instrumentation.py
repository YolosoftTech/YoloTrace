"""Optional instrumentation that records LLM client calls as spans."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from yolotrace.sdk.tracing import Span, trace

_MARKER = "__yolotrace_instrumented__"


def instrument_openai() -> None:
    """Record each ``chat.completions.create`` call of the ``openai`` package as an LLM span."""
    try:
        from openai.resources.chat.completions import Completions
    except ImportError as exc:
        raise ImportError("instrument_openai() requires the 'openai' package") from exc

    _wrap(
        Completions,
        "openai.chat.completions.create",
        "openai",
        "prompt_tokens",
        "completion_tokens",
    )


def instrument_anthropic() -> None:
    """Record each ``messages.create`` call of the ``anthropic`` package as an LLM span."""
    try:
        from anthropic.resources.messages import Messages
    except ImportError as exc:
        raise ImportError(
            "instrument_anthropic() requires the 'anthropic' package"
        ) from exc

    _wrap(
        Messages,
        "anthropic.messages.create",
        "anthropic",
        "input_tokens",
        "output_tokens",
    )


def _wrap(
    owner: type[Any],
    span_name: str,
    provider: str,
    input_field: str,
    output_field: str,
) -> None:
    original: Callable[..., Any] = owner.create
    if getattr(original, _MARKER, False):
        return

    @functools.wraps(original)
    def create(self: Any, *args: Any, **kwargs: Any) -> Any:
        attributes = {"provider": provider, "model": kwargs.get("model")}
        with trace(span_name, kind="llm", attributes=attributes) as span:
            response = original(self, *args, **kwargs)
            _record_usage(span, response, input_field, output_field)
            return response

    setattr(create, _MARKER, True)
    owner.create = create  # type: ignore[method-assign]


def _record_usage(
    span: Span, response: Any, input_field: str, output_field: str
) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    input_tokens = getattr(usage, input_field, None)
    output_tokens = getattr(usage, output_field, None)
    if input_tokens is not None:
        span.set_attribute("input_tokens", input_tokens)
    if output_tokens is not None:
        span.set_attribute("output_tokens", output_tokens)
