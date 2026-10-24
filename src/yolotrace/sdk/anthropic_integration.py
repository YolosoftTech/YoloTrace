"""Records Anthropic message calls as LLM spans.

This module imports ``anthropic`` directly, so it is only imported by
``instrument_anthropic``, which turns the missing-package case into a clear error.
"""

from __future__ import annotations

from typing import cast

from anthropic.resources.messages import AsyncMessages, Messages
from anthropic.types import Message, RawMessageDeltaEvent, RawMessageStartEvent

from yolotrace.sdk.llm_calls import (
    AsyncCreate,
    Provider,
    SyncCreate,
    TokenCounts,
    is_wrapped,
    wrap_create,
    wrap_create_async,
)


def _read_tokens(response: object) -> TokenCounts:
    """Read token counts from a message or stream event.

    Anthropic reports usage on the message in ``message_start`` and on ``usage`` in
    ``message_delta``. The delta carries no input count when the start event already has it.
    """
    if isinstance(response, Message):
        return response.usage.input_tokens, response.usage.output_tokens
    if isinstance(response, RawMessageStartEvent):
        usage = response.message.usage
        return usage.input_tokens, usage.output_tokens
    if isinstance(response, RawMessageDeltaEvent):
        return response.usage.input_tokens, response.usage.output_tokens
    return None, None


_PROVIDER = Provider(
    name="anthropic",
    span_name="anthropic.messages.create",
    read_tokens=_read_tokens,
)


def install() -> None:
    """Record every message call of the Anthropic sync and async clients. Calling it again does nothing."""
    # The SDK's overloads name each keyword argument, which the wrapper forwards unchanged.
    if not is_wrapped(Messages.create):
        Messages.create = wrap_create(  # type: ignore[assignment]
            cast(SyncCreate, Messages.create), _PROVIDER
        )
    if not is_wrapped(AsyncMessages.create):
        AsyncMessages.create = wrap_create_async(  # type: ignore[assignment]
            cast(AsyncCreate, AsyncMessages.create), _PROVIDER
        )
