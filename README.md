# YoloTrace

Open-source, self-hosted observability and debugging for AI agents.

YoloTrace records what your AI agents did, why they did it, where they failed, and how much they cost, while keeping your trace data on infrastructure you control.

> **Status:** early development. Nothing here is ready for production use yet. See [product.md](product.md) for the product vision and backlog.

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

To be decided before the first public release. See [product.md](product.md), section 10.
