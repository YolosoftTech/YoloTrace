# YoloTrace

Open-source, self-hosted observability and debugging for AI agents.

YoloTrace records what your AI agents did, why they did it, where they failed, and how much they cost, while keeping your trace data on infrastructure you control.

> **Status:** early development. The Python and JavaScript/TypeScript SDKs' tracing APIs work today. It is not ready for production use.

## What works today

### Tracing with the Python SDK

Use `trace` as a decorator for a function, or as a context manager for a block of code:

```python
from yolotrace import finished_spans, trace


@trace()
def answer(question: str) -> str:
    with trace("lookup"):
        ...
    return "42"


answer("What is the answer?")

for span in finished_spans():
    print(span.name, span.status, span.duration)
```

For work that does not fit one block or function, start a span explicitly and end it later:

```python
from yolotrace import start_span

job = start_span("upload", kind="tool", attributes={"bytes": 1024})
# ... later, possibly in another function ...
job.end()  # or job.end(exc) to record a failure
```

To record OpenAI and Anthropic calls automatically, opt in once at startup. Each `chat.completions.create` (OpenAI) or `messages.create` (Anthropic) call becomes an `llm` span with the provider, model, and token counts:

```python
from yolotrace import instrument_anthropic, instrument_openai

instrument_openai()  # requires the openai package
instrument_anthropic()  # requires the anthropic package
```

Calling them more than once is safe. Async clients are instrumented too. A streaming call (`stream=True`) is one span that ends when the stream is exhausted or closed, and its token counts come from the stream's usage events.

`@trace()` also works on `async def` functions and on generator and async generator functions, where the span covers the whole stream. Concurrent tasks each keep their own parent span.

- Nested spans link to their parent through `parent_id`.
- `running_spans()` lists spans that have started but not ended, so long-running workflows are visible while they run.
- Threads and executors do not copy context, so wrap the work with `propagate_context` in the thread that starts it. Spans it records are then parented to the current span. Asyncio tasks already keep their parent.

```python
from concurrent.futures import ThreadPoolExecutor

from yolotrace import propagate_context, trace


@trace()
def process(item: str) -> None:
    ...


@trace("batch")
def run_batch(items: list[str]) -> None:
    with ThreadPoolExecutor() as pool:
        for item in items:
            pool.submit(propagate_context(process), item)
```
- Exceptions are recorded on the span (`status == "error"`, `error == "Type: message"`) and re-raised unchanged.
- Finished spans are kept in process memory. Storage and the ingestion API are not built yet.

To record agent framework runs, opt in once at startup. Each package is optional and must be installed:

```python
from yolotrace import instrument_langchain, instrument_openai_agents

instrument_langchain()  # requires langchain-core; records every LangChain run in the process
instrument_openai_agents()  # requires openai-agents; records every Agents SDK trace in the process
```

- LangChain chains become `run` (top level) and `step` spans, model calls become `llm` spans, and tool calls become `tool` spans.
- Agents SDK traces become `run` spans. Agent and handoff spans become `step` spans, model calls become `llm` spans, and function tool calls become `tool` spans.
- **Privacy:** the Agents SDK keeps its own exporter. When `OPENAI_API_KEY` is set, it sends its traces to OpenAI. To keep traces local, leave `OPENAI_API_KEY` unset in the process that runs agents; the SDK then skips the export and YoloTrace still records the spans. Setting `OPENAI_AGENTS_DISABLE_TRACING=1` stops the export too, but it also stops YoloTrace from receiving any spans.

### Tracing across services and processes

To continue a trace in another service or process, write the current trace context into a carrier, such as HTTP headers or a job payload, and read it on the other side:

```python
from yolotrace import continue_trace, extract, inject, trace

# Caller process
with trace("request") as request:
    headers: dict[str, str] = {}
    inject(headers)  # adds a "traceparent" entry, in the W3C Trace Context format
    send_to_worker(headers)

# Worker process
context = extract(headers)  # None if the entry is missing or malformed
with continue_trace(context), trace("job") as job:
    ...  # job.trace_id == request.trace_id and job.parent_id == request.span_id
```

Spans recorded in the other process share the same `trace_id`. Each carrier key is `traceparent` in lower case, so normalize HTTP header names before calling `extract`.

### Sampling and capture limits

Use `configure` once at startup to record only part of the traffic or to cap how much each span keeps:

```python
from yolotrace import configure

configure(sample_rate=0.1, max_attributes=64, max_attribute_length=2048)
```

- `sample_rate` is the fraction of new traces to record, from `0.0` to `1.0`. The choice is made where a trace starts, and every span of that trace follows it, including spans in other processes that continue it with `extract`. Unsampled spans still run, but they are not kept by `finished_spans` or `running_spans`.
- `max_attributes` caps the attributes a span keeps. Extra keys are dropped.
- `max_attribute_length` caps string attributes and error messages. Longer values are cut short.

Each call replaces every setting, and omitted settings return to their defaults: record everything, 128 attributes, and 4096 characters.

### JavaScript/TypeScript SDK

The JavaScript/TypeScript SDK lives in `sdk/javascript` and needs Node.js 20 or later. It has no runtime dependencies. It offers the same tracing API as the Python SDK, with functions instead of decorators:

```typescript
import { configure, finishedSpans, inject, startSpan, trace } from "yolotrace";

configure({ sampleRate: 0.1, maxAttributes: 64, maxAttributeLength: 2048 });

await trace("answer", async () => {
  await trace("lookup", () => {}, { kind: "tool" });
}, { kind: "run" });

const job = startSpan("upload", { kind: "tool", attributes: { bytes: 1024 } });
job.end(); // or job.end(error) to record a failure

const headers: Record<string, string> = {};
trace("request", () => inject(headers));
```

- `trace(name, fn, options)` runs `fn` as a span. It works with sync functions and with promises, and the span stays current across `await`s. A thrown error is recorded and rethrown unchanged.
- `continueTrace(extract(headers), fn)` continues a trace from another process, the same as `continue_trace` and `extract` in Python.
- Streams and async generators are not traced yet. Automatic instrumentation of LLM clients is not available in this SDK.

## Architecture

YoloTrace is a **pragmatic modular monolith**: one deployable Python application, organized into modules with explicit boundaries. Modules talk through their public interfaces rather than reaching into each other's internals, so they can be split out later if real load demands it.

## Development setup

Requirements: [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
uv sync
uv run pytest
uv run ruff check
uv run ruff format --check
```

## License

To be decided before the first public release.
