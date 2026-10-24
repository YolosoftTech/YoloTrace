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
