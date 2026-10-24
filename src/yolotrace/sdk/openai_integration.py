"""Records OpenAI chat completion calls as LLM spans.

This module imports ``openai`` directly, so it is only imported by ``instrument_openai``,
which turns the missing-package case into a clear error.
"""

from __future__ import annotations

from typing import cast

from openai.resources.chat.completions import AsyncCompletions, Completions
from openai.types.chat import ChatCompletion, ChatCompletionChunk

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
    """Read token counts from a completion or stream chunk, which OpenAI reports as ``usage``."""
    if not isinstance(response, ChatCompletion | ChatCompletionChunk):
        return None, None
    if response.usage is None:
        return None, None
    return response.usage.prompt_tokens, response.usage.completion_tokens


_PROVIDER = Provider(
    name="openai",
    span_name="openai.chat.completions.create",
    read_tokens=_read_tokens,
)


def install() -> None:
    """Record every chat completion of the OpenAI sync and async clients. Calling it again does nothing."""
    # The SDK's overloads name each keyword argument, which the wrapper forwards unchanged.
    if not is_wrapped(Completions.create):
        Completions.create = wrap_create(  # type: ignore[assignment]
            cast(SyncCreate, Completions.create), _PROVIDER
        )
    if not is_wrapped(AsyncCompletions.create):
        AsyncCompletions.create = wrap_create_async(  # type: ignore[assignment]
            cast(AsyncCreate, AsyncCompletions.create), _PROVIDER
        )
