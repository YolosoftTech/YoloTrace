"""Public Python SDK for recording AI agent activity."""

from yolotrace.sdk.export import ExportError, export_spans
from yolotrace.sdk.instrumentation import (
    instrument_anthropic,
    instrument_langchain,
    instrument_openai,
    instrument_openai_agents,
)
from yolotrace.sdk.policy import (
    InstrumentationPolicy,
    PolicyError,
    apply_policy,
    load_policy,
)
from yolotrace.sdk.tracing import (
    Run,
    Span,
    SpanContext,
    SpanEvent,
    SpanKind,
    configure,
    continue_trace,
    current_span,
    extract,
    finished_spans,
    inject,
    propagate_context,
    running_spans,
    runs,
    start_span,
    trace,
)

__version__ = "0.1.0"

__all__ = [
    "ExportError",
    "InstrumentationPolicy",
    "PolicyError",
    "Run",
    "Span",
    "SpanContext",
    "SpanEvent",
    "SpanKind",
    "__version__",
    "apply_policy",
    "configure",
    "continue_trace",
    "current_span",
    "export_spans",
    "extract",
    "finished_spans",
    "inject",
    "instrument_anthropic",
    "instrument_langchain",
    "instrument_openai",
    "instrument_openai_agents",
    "load_policy",
    "propagate_context",
    "running_spans",
    "runs",
    "start_span",
    "trace",
]
