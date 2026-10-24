"""OpenAI Agents SDK trace processor that records agent traces and their spans.

This module imports ``agents`` directly, so it is only imported by
``instrument_openai_agents``, which turns the missing-package case into a clear error.
"""

from __future__ import annotations

from agents import add_trace_processor
from agents.tracing import Span as AgentSpan
from agents.tracing import Trace, TracingProcessor
from agents.tracing.span_data import (
    AgentSpanData,
    FunctionSpanData,
    GenerationSpanData,
    HandoffSpanData,
    ResponseSpanData,
    SpanData,
)

from yolotrace.sdk.llm_calls import mark_llm_call_finished, mark_llm_call_started
from yolotrace.sdk.tracing import AttributeValue, Span, SpanKind, start_span


class YoloTraceTracingProcessor(TracingProcessor):
    """Map each Agents SDK trace to a "run" span and each of its spans to a child span.

    Agent and handoff spans become "step" spans, model calls become "llm" spans and
    function tool calls become "tool" spans.
    """

    def __init__(self) -> None:
        self._spans: dict[str, Span] = {}
        self._llm_marks: dict[str, bool] = {}

    def on_trace_start(self, trace: Trace) -> None:
        """Start a "run" span for an Agents SDK trace."""
        self._spans[trace.trace_id] = start_span(trace.name, kind="run")

    def on_trace_end(self, trace: Trace) -> None:
        """Finish the "run" span of an Agents SDK trace."""
        self._finish(trace.trace_id)

    def on_span_start(self, span: AgentSpan[SpanData]) -> None:
        """Start a child span under the span or trace the Agents SDK reports as its parent."""
        name, kind, attributes = _describe(span.span_data)
        child = start_span(name, kind=kind, attributes=attributes)
        parent = self._spans.get(span.parent_id or span.trace_id)
        if parent is not None:
            child.parent_id = parent.span_id
            child.trace_id = parent.trace_id
        self._spans[span.span_id] = child
        if kind == "llm":
            # The provider SDK call made by this model span is recorded by this span, not again.
            self._llm_marks[span.span_id] = mark_llm_call_started()

    def on_span_end(self, span: AgentSpan[SpanData]) -> None:
        """Finish a child span, recording token usage and any error the Agents SDK reports."""
        child = self._spans.get(span.span_id)
        if child is None:
            return
        # The SDK fills in some values, such as a handoff's target agent, only while the span runs.
        _, _, attributes = _describe(span.span_data)
        child.attributes.update(attributes)
        _record_usage(child, span.span_data)
        if span.error is not None:
            child.status = "error"
            child.error = span.error["message"]
        previous = self._llm_marks.pop(span.span_id, None)
        if previous is not None:
            mark_llm_call_finished(previous)
        self._finish(span.span_id)

    def shutdown(self) -> None:
        """Nothing to release; finished spans stay in the process-wide span store."""

    def force_flush(self) -> None:
        """Nothing to flush; spans are stored as soon as they finish."""

    def _finish(self, key: str) -> None:
        span = self._spans.pop(key, None)
        if span is not None:
            span.end()


def _describe(data: SpanData) -> tuple[str, SpanKind, dict[str, AttributeValue]]:
    if isinstance(data, GenerationSpanData):
        return "openai_agents.generation", "llm", {"model": data.model}
    if isinstance(data, ResponseSpanData):
        return "openai_agents.response", "llm", {}
    if isinstance(data, FunctionSpanData):
        return data.name, "tool", {"tool_name": data.name}
    if isinstance(data, HandoffSpanData):
        attributes: dict[str, AttributeValue] = {
            "from_agent": data.from_agent,
            "to_agent": data.to_agent,
        }
        return "handoff", "step", attributes
    if isinstance(data, AgentSpanData):
        return data.name, "step", {}
    return data.type, "step", {}


def _record_usage(span: Span, data: SpanData) -> None:
    if not isinstance(data, GenerationSpanData | ResponseSpanData) or not data.usage:
        return
    input_tokens = data.usage.get("input_tokens")
    output_tokens = data.usage.get("output_tokens")
    if isinstance(input_tokens, int):
        span.set_attribute("input_tokens", input_tokens)
    if isinstance(output_tokens, int):
        span.set_attribute("output_tokens", output_tokens)


_installed = False


def install() -> None:
    """Register the processor with the Agents SDK. Calling it again does nothing."""
    global _installed
    if _installed:
        return
    add_trace_processor(YoloTraceTracingProcessor())
    _installed = True
