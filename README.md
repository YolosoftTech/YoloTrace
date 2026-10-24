# YoloTrace

Open-source, self-hosted observability and debugging for AI agents.

YoloTrace records what your AI agents did, why they did it, where they failed, and how much they cost, while keeping your trace data on infrastructure you control.

> **Status:** early development. Only the Python SDK's tracing API works today. It is not ready for production use.

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

Calling them more than once is safe. Streaming (`stream=True`) and async calls are not instrumented yet.

- Nested spans link to their parent through `parent_id`.
- Exceptions are recorded on the span (`status == "error"`, `error == "Type: message"`) and re-raised unchanged.
- Finished spans are kept in process memory. Storage and the ingestion API are not built yet.

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
