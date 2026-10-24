import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Generator, Iterator

import pytest

from yolotrace.sdk import current_span, finished_spans, trace


async def test_async_decorator_ends_span_after_awaiting_the_call() -> None:
    @trace("fetch", kind="tool")
    async def fetch() -> str:
        await asyncio.sleep(0)
        return "data"

    coroutine = fetch()
    assert finished_spans() == []

    assert await coroutine == "data"
    (span,) = finished_spans()
    assert span.name == "fetch"
    assert span.kind == "tool"
    assert span.status == "ok"
    assert span.end_time is not None


async def test_async_decorator_keeps_function_metadata() -> None:
    @trace()
    async def load() -> None:
        """Load things."""

    assert load.__name__ == "load"
    assert load.__doc__ == "Load things."
    assert asyncio.iscoroutinefunction(load)


async def test_async_decorator_records_exception_and_reraises() -> None:
    @trace()
    async def fail() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await fail()

    (span,) = finished_spans()
    assert span.status == "error"
    assert span.error == "RuntimeError: boom"


async def test_async_decorator_links_nested_spans_and_restores_context() -> None:
    @trace("child")
    async def child() -> None:
        await asyncio.sleep(0)

    @trace("parent")
    async def parent() -> None:
        await child()
        assert current_span() is not None
        assert current_span().name == "parent"  # type: ignore[union-attr]

    await parent()
    assert current_span() is None
    child_span, parent_span = finished_spans()
    assert child_span.parent_id == parent_span.span_id
    assert parent_span.parent_id is None


async def test_async_context_manager_records_block() -> None:
    async with trace("block", kind="step", attributes={"k": 1}) as span:
        assert span.end_time is None
        await asyncio.sleep(0)

    (recorded,) = finished_spans()
    assert recorded is span
    assert recorded.end_time is not None
    assert recorded.attributes == {"k": 1}


async def test_async_context_manager_records_exception_and_reraises() -> None:
    with pytest.raises(KeyError):
        async with trace("lookup"):
            raise KeyError("missing")

    (span,) = finished_spans()
    assert span.status == "error"
    assert span.error == "KeyError: 'missing'"
    assert current_span() is None


async def test_async_context_manager_requires_name() -> None:
    with pytest.raises(ValueError, match="span name is required"):
        async with trace():
            pass


def test_sync_generator_span_ends_when_exhausted() -> None:
    @trace("tokens", kind="llm")
    def tokens() -> Iterator[str]:
        yield "a"
        yield "b"

    stream = tokens()
    assert finished_spans() == []

    assert list(stream) == ["a", "b"]
    (span,) = finished_spans()
    assert span.name == "tokens"
    assert span.kind == "llm"
    assert span.status == "ok"
    assert span.end_time is not None


def test_sync_generator_is_current_span_only_while_it_runs() -> None:
    seen: list[str | None] = []

    @trace("stream")
    def gen() -> Iterator[int]:
        span = current_span()
        seen.append(span.name if span else None)
        yield 1
        span = current_span()
        seen.append(span.name if span else None)
        yield 2

    for _ in gen():
        assert current_span() is None

    assert seen == ["stream", "stream"]


def test_sync_generator_child_spans_are_parented_to_stream() -> None:
    @trace("inner")
    def inner() -> None:
        pass

    @trace("stream")
    def gen() -> Iterator[int]:
        inner()
        yield 1

    assert list(gen()) == [1]
    inner_span, stream_span = finished_spans()
    assert inner_span.parent_id == stream_span.span_id


def test_sync_generator_records_error_and_reraises() -> None:
    @trace("stream")
    def gen() -> Iterator[int]:
        yield 1
        raise ConnectionError("dropped")

    with pytest.raises(ConnectionError, match="dropped"):
        list(gen())

    (span,) = finished_spans()
    assert span.status == "error"
    assert span.error == "ConnectionError: dropped"


def test_sync_generator_closed_early_ends_span() -> None:
    @trace("stream")
    def gen() -> Generator[int, None, None]:
        yield 1
        yield 2

    stream = gen()
    assert next(stream) == 1
    stream.close()

    (span,) = finished_spans()
    assert span.status == "ok"
    assert span.end_time is not None
    assert current_span() is None


def test_empty_sync_generator_still_records_span() -> None:
    @trace("empty")
    def gen() -> Iterator[int]:
        return
        yield  # pragma: no cover

    assert list(gen()) == []
    (span,) = finished_spans()
    assert span.name == "empty"


async def test_async_generator_span_ends_when_exhausted() -> None:
    @trace("chunks", kind="llm")
    async def chunks() -> AsyncIterator[str]:
        yield "a"
        await asyncio.sleep(0)
        yield "b"

    received = [item async for item in chunks()]

    assert received == ["a", "b"]
    (span,) = finished_spans()
    assert span.name == "chunks"
    assert span.status == "ok"
    assert span.end_time is not None
    assert current_span() is None


async def test_async_generator_is_current_span_only_while_it_runs() -> None:
    seen: list[str | None] = []

    @trace("stream")
    async def gen() -> AsyncIterator[int]:
        span = current_span()
        seen.append(span.name if span else None)
        yield 1
        span = current_span()
        seen.append(span.name if span else None)
        await asyncio.sleep(0)
        yield 2

    async for _ in gen():
        assert current_span() is None

    assert seen == ["stream", "stream"]


async def test_async_generator_records_error_and_reraises() -> None:
    @trace("stream")
    async def gen() -> AsyncIterator[int]:
        yield 1
        raise TimeoutError("slow")

    with pytest.raises(TimeoutError, match="slow"):
        [item async for item in gen()]

    (span,) = finished_spans()
    assert span.status == "error"
    assert span.error == "TimeoutError: slow"


async def test_async_generator_closed_early_ends_span() -> None:
    @trace("stream")
    async def gen() -> AsyncGenerator[int, None]:
        yield 1
        yield 2

    stream = gen()
    assert await stream.__anext__() == 1
    await stream.aclose()

    (span,) = finished_spans()
    assert span.end_time is not None


async def test_concurrent_tasks_are_parented_to_their_own_task_span() -> None:
    @trace("step")
    async def step() -> None:
        await asyncio.sleep(0)

    @trace("worker")
    async def worker() -> None:
        await step()
        await asyncio.sleep(0)
        await step()

    with trace("batch") as batch:
        await asyncio.gather(worker(), worker(), worker())

    spans = finished_spans()
    workers = [span for span in spans if span.name == "worker"]
    steps = [span for span in spans if span.name == "step"]
    assert len(workers) == 3
    assert len(steps) == 6
    assert all(worker.parent_id == batch.span_id for worker in workers)
    worker_ids = {worker.span_id for worker in workers}
    assert all(step.parent_id in worker_ids for step in steps)
    for worker_span in workers:
        children = [step for step in steps if step.parent_id == worker_span.span_id]
        assert len(children) == 2


async def test_interleaved_async_generators_keep_their_own_parents() -> None:
    @trace("left")
    async def left() -> AsyncGenerator[str, None]:
        yield "l1"
        await asyncio.sleep(0)
        yield "l2"

    @trace("right")
    async def right() -> AsyncGenerator[str, None]:
        yield "r1"
        await asyncio.sleep(0)
        yield "r2"

    left_stream = left()
    right_stream = right()
    pairs = [
        (await left_stream.__anext__(), await right_stream.__anext__()),
        (await left_stream.__anext__(), await right_stream.__anext__()),
    ]

    assert pairs == [("l1", "r1"), ("l2", "r2")]
    assert current_span() is None
    assert finished_spans() == []
    await left_stream.aclose()
    await right_stream.aclose()
    spans = finished_spans()
    assert sorted(span.name for span in spans) == ["left", "right"]
    assert all(span.parent_id is None for span in spans)


async def test_async_block_inside_concurrent_tasks_does_not_leak() -> None:
    async def task(tag: str) -> str:
        async with trace(tag) as span:
            await asyncio.sleep(0)
            assert current_span() is span
        return span.name

    names = await asyncio.gather(task("a"), task("b"))

    assert list(names) == ["a", "b"]
    assert current_span() is None
    assert all(span.parent_id is None for span in finished_spans())
