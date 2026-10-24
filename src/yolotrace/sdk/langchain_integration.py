"""LangChain callback handler that records chains, model calls and tool calls as spans.

This module imports ``langchain_core`` directly, so it is only imported by
``instrument_langchain``, which turns the missing-package case into a clear error.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult
from langchain_core.tracers.context import register_configure_hook

from yolotrace.sdk.tracing import Span, SpanKind, start_span


class YoloTraceCallbackHandler(BaseCallbackHandler):
    """Record LangChain runs as spans, linked to their parent through LangChain's run IDs.

    A chain without a parent becomes a "run" span; nested chains become "step" spans,
    model calls become "llm" spans and tool calls become "tool" spans.
    """

    def __init__(self) -> None:
        self._spans: dict[UUID, Span] = {}

    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Start a span for a chain, as a "run" span when it has no parent."""
        kind: SpanKind = "run" if parent_run_id is None else "step"
        self._start(run_id, parent_run_id, kwargs.get("name") or "chain", kind)

    def on_chain_end(
        self, outputs: dict[str, Any], *, run_id: UUID, **kwargs: Any
    ) -> None:
        """Finish the span of a chain that completed."""
        self._finish(run_id)

    def on_chain_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: Any
    ) -> None:
        """Finish the span of a chain that raised, recording the error."""
        self._finish(run_id, error)

    def on_chat_model_start(
        self,
        serialized: dict[str, Any] | None,
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Start an "llm" span for a chat model call."""
        self._start_model(run_id, parent_run_id, **kwargs)

    def on_llm_start(
        self,
        serialized: dict[str, Any] | None,
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Start an "llm" span for a text completion call."""
        self._start_model(run_id, parent_run_id, **kwargs)

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        """Finish a model span, recording token counts when the provider reports them."""
        span = self._spans.get(run_id)
        if span is not None:
            _record_usage(span, response)
        self._finish(run_id)

    def on_llm_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: Any
    ) -> None:
        """Finish a model span that raised, recording the error."""
        self._finish(run_id, error)

    def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Start a "tool" span for a tool call."""
        name = kwargs.get("name") or (serialized or {}).get("name") or "tool"
        self._start(run_id, parent_run_id, name, "tool", {"tool_name": name})

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        """Finish the span of a tool call that completed."""
        self._finish(run_id)

    def on_tool_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: Any
    ) -> None:
        """Finish the span of a tool call that raised, recording the error."""
        self._finish(run_id, error)

    def _start_model(
        self, run_id: UUID, parent_run_id: UUID | None, **kwargs: Any
    ) -> None:
        params = kwargs.get("invocation_params") or {}
        self._start(
            run_id,
            parent_run_id,
            kwargs.get("name") or "llm",
            "llm",
            {"model": params.get("model")},
        )

    def _start(
        self,
        run_id: UUID,
        parent_run_id: UUID | None,
        name: str,
        kind: SpanKind,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        span = start_span(name, kind=kind, attributes=attributes)
        parent = None if parent_run_id is None else self._spans.get(parent_run_id)
        if parent is not None:
            span.parent_id = parent.span_id
        self._spans[run_id] = span

    def _finish(self, run_id: UUID, error: BaseException | None = None) -> None:
        span = self._spans.pop(run_id, None)
        if span is not None:
            span.end(error)


def _record_usage(span: Span, response: LLMResult) -> None:
    for generations in response.generations:
        for generation in generations:
            usage = getattr(
                getattr(generation, "message", None), "usage_metadata", None
            )
            if usage:
                span.set_attribute("input_tokens", usage["input_tokens"])
                span.set_attribute("output_tokens", usage["output_tokens"])
                return


_handler = YoloTraceCallbackHandler()
_handler_var: ContextVar[BaseCallbackHandler | None] = ContextVar(
    "yolotrace_langchain_handler", default=_handler
)
_installed = False


def install() -> None:
    """Attach the handler to every LangChain run in the process. Calling it again does nothing."""
    global _installed
    if _installed:
        return
    register_configure_hook(_handler_var, inheritable=True)
    _installed = True
