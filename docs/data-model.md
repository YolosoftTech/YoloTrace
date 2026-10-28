# YoloTrace data model

This document defines the trace, span, event, and attribute model that YoloTrace records. It describes the Python SDK (`yolotrace.sdk.tracing`), which is the reference implementation. Field names are the Python names; the JSON format used by export and ingestion uses the same names (see [docs/ingestion-api.md](ingestion-api.md)).

## Overview

```text
Trace (trace_id)
└── Span (span_id, parent_id)        kind: run | step | llm | tool
    ├── attributes                   key → primitive value
    ├── events                       ordered points in time
    │   └── SpanEvent (name, time, attributes)
    └── Span (child)                 parent_id = parent span_id
```

- A **trace** is every span that shares one `trace_id`. It may cover several processes and services when one continues another's trace (see [Tracing across services and processes](../README.md)).
- A **span** is one unit of work with a start, an optional end, and a status.
- An **event** is a named point in time inside a span.
- An **attribute** is a named primitive value that describes a span or an event.

## Trace

A trace has no record of its own. It is the shared `trace_id` of its spans.

| Property | Type | Rules |
| --- | --- | --- |
| `trace_id` | `str` | 32 lowercase hexadecimal characters (16 random bytes). Set when the trace starts and never changed. |

The top-level unit of a trace is its root span: the span whose `parent_id` is `None` in this process. Spans with `kind="run"` mark a whole agent run. A trace is a run in the common case.

## Span

A span is one recorded unit of work.

| Field | Type | Rules |
| --- | --- | --- |
| `name` | `str` | Human-readable name. `@trace()` uses the function's qualified name. |
| `trace_id` | `str` | Same as the trace the span belongs to. |
| `span_id` | `str` | 16 lowercase hexadecimal characters (8 random bytes). Unique within the trace. |
| `parent_id` | `str \| None` | `span_id` of the parent span, or `None` for a root span. A parent may be recorded in another process when the trace was continued with `extract`. |
| `kind` | `"run" \| "step" \| "llm" \| "tool"` | What the span represents. Default `"step"`. |
| `start_time` | `float` | Unix time in seconds, from `time.time()`. |
| `end_time` | `float \| None` | Unix time in seconds. `None` while the span is running. Set once. |
| `duration` | `float \| None` | Seconds from start to end, measured with a monotonic clock so a system clock change cannot make it negative. `None` while running. It can differ slightly from `end_time - start_time`. |
| `status` | `str` | `"ok"` (default) or `"error"`. |
| `error` | `str \| None` | `"Type: message"` for the exception that ended the span with an error. Cut to `max_attribute_length`. `None` otherwise. |
| `attributes` | `dict[str, AttributeValue]` | Metadata about the span. See [Attributes](#attributes). |
| `events` | `list[SpanEvent]` | Points in time inside the span, in the order they were added. See [Event](#event). |
| `sampled` | `bool` | Whether the span is recorded. Set where the trace starts, so one trace is either recorded whole or not at all. |

### Span lifecycle

1. **Start.** The span gets its IDs and `start_time`. It is parented to the current span, or to a remote context from `continue_trace`.
2. **Running.** Attributes and events may be added. A running span is listed by `running_spans()`.
3. **End.** `end()` sets `end_time`. If the span ends because of an exception, `end(exc)` sets `status="error"` and `error`. Ending a span twice raises `RuntimeError`.
4. **Finished.** A sampled, ended span is listed by `finished_spans()`. Unsampled spans are never listed.

### Parent and child rules

- A span created while another span is current has that span as its parent and shares its `trace_id`.
- A span is current only inside its `with` block, its decorated call, or the running part of its generator. `start_span` does not make its span current.
- Sibling spans run in order or concurrently. Concurrent asyncio tasks each keep their own parent. Threads need `propagate_context`.

## Run

A run is the finished spans of one trace, grouped so that parents and children can be walked. `runs()` returns them in Python, and `runs()` and `Runs()` do the same in the JavaScript and Go SDKs. Grouping happens when it is called and stores nothing new.

| Field | Type | Rules |
| --- | --- | --- |
| `trace_id` | `str` | The shared `trace_id` of the run's spans. Identifies the run. |
| `spans` | `tuple[Span, ...]` | Every finished, sampled span with that `trace_id`, in start order. |
| `roots` | `tuple[Span, ...]` | Spans whose `parent_id` is not the `span_id` of another span in the run. |

- `run.children(span)` returns the spans of the run whose `parent_id` is `span.span_id`, in start order.
- A span is a root when its parent is not part of the run. That covers the top-level span, a span whose parent was recorded in another process, and a span whose parent has not ended yet. Once the parent ends, the child is no longer a root.
- A run can have more than one root when one trace continues in several places. Its spans still form one run.
- Runs are listed in the order their first span started. Running spans and unsampled spans are not part of any run.
- Spans that started at the same clock reading keep the order in which they ended.

## Event

An event records something that happened at one instant inside a span, such as a retry, a cache miss, or a checkpoint. Use an event when the thing has no duration of its own. Use a child span when it does.

| Field | Type | Rules |
| --- | --- | --- |
| `name` | `str` | What happened, such as `"retry"`. |
| `time` | `float` | Unix time in seconds, from `time.time()`, when `add_event` was called. |
| `attributes` | `dict[str, AttributeValue]` | Details of the event. Same rules as span attributes. |

- Events are created with `span.add_event(name, attributes)` in Python, `span.addEvent(name, attributes)` in JavaScript/TypeScript, and `span.AddEvent(name, attributes)` in Go.
- Events are immutable once created (`SpanEvent` is a frozen dataclass).
- Events are ordered by the time they were added. An event added to an ended span is stored on that span.
- An event's `time` falls between the span's `start_time` and `end_time` when it is added during the span.

## Attributes

An attribute is a key with a primitive value that can be stored and exported as-is.

- **Keys** are `str`.
- **Values** have the type `AttributeValue = str | int | float | bool | None`. Nested objects, lists, and other types are not stored; convert them to a string first.
- **Span attributes** are set with `trace(..., attributes=...)`, `start_span(..., attributes=...)`, or `span.set_attribute(key, value)`. Setting an existing key replaces its value.
- **Event attributes** are set with `span.add_event(name, attributes)`. An event's attributes are independent of its span's attributes.
- **Limits** are set with `configure()` (or a policy file). `max_attributes` is the most keys one span or one event keeps. `max_attribute_length` is the longest string kept. Keys beyond the limit are dropped, and longer strings are cut short. Neither raises an error, so instrumentation never fails the application.

Defaults: 128 attributes per span or event, 4096 characters per string.

## Status and errors

- `status` is `"ok"` unless the span ended with an exception, in which case it is `"error"`.
- `error` holds the exception type and message, such as `"ValueError: bad input"`.
- The exception is re-raised unchanged by the `trace` decorator and context manager.

## Sampling and capture limits

Sampling decides, once per trace, whether its spans are kept. Unsampled spans still run, but they never appear in `finished_spans()` or `running_spans()`. Capture limits apply to every recorded span and event. See the [README](../README.md#sampling-and-capture-limits).

## Stable identifiers

- `trace_id` and `span_id` are random, so they are unique without coordination.
- A trace is continued across processes by carrying `traceparent` (W3C Trace Context format). The span IDs in it are the parent's `span_id` values.

## Not yet defined

These parts of the model are planned but not implemented:

- Mapping to OpenTelemetry span and event fields.
- Limits on the number of events per span.
