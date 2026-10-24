import asyncio
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from yolotrace.sdk import (
    current_span,
    finished_spans,
    propagate_context,
    running_spans,
    start_span,
    trace,
)


def test_running_spans_lists_work_until_it_ends() -> None:
    span = start_span("export", kind="tool")

    assert running_spans() == [span]
    assert finished_spans() == []

    span.end()

    assert running_spans() == []
    assert finished_spans() == [span]


def test_running_spans_shows_an_open_block_in_start_order() -> None:
    with trace("workflow") as workflow, trace("step") as step:
        assert running_spans() == [workflow, step]

    assert running_spans() == []


def test_running_spans_keeps_unfinished_generator_until_closed() -> None:
    @trace("stream")
    def gen() -> Iterator[int]:
        yield 1
        yield 2

    stream = gen()
    assert next(stream) == 1
    (running,) = running_spans()
    assert running.name == "stream"

    stream.close()

    assert running_spans() == []


def test_thread_job_without_propagation_loses_parent() -> None:
    done = threading.Event()

    def job() -> None:
        with trace("job"):
            pass
        done.set()

    with trace("run") as run:
        thread = threading.Thread(target=job)
        thread.start()
        done.wait(timeout=5)
        thread.join()

    job_span = next(span for span in finished_spans() if span.name == "job")
    assert job_span.parent_id is None
    assert run.span_id not in {span.parent_id for span in finished_spans()}


def test_thread_job_with_propagation_is_parented_to_caller() -> None:
    def job() -> str:
        with trace("job") as span:
            return span.span_id

    with trace("run") as run:
        thread_job = propagate_context(job)
        with ThreadPoolExecutor(max_workers=1) as pool:
            job_id = pool.submit(thread_job).result()

    job_span = next(span for span in finished_spans() if span.span_id == job_id)
    assert job_span.parent_id == run.span_id


def test_propagated_job_records_exception_and_reraises() -> None:
    def job() -> None:
        with trace("job"):
            raise RuntimeError("worker crashed")

    with trace("run"):
        thread_job = propagate_context(job)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(thread_job)
            with pytest.raises(RuntimeError, match="worker crashed"):
                future.result()

    job_span = next(span for span in finished_spans() if span.name == "job")
    assert job_span.status == "error"
    assert job_span.error == "RuntimeError: worker crashed"


def test_propagated_job_keeps_function_metadata() -> None:
    def load(path: str) -> str:
        """Load a file."""
        return path

    wrapped: Callable[[str], str] = propagate_context(load)

    assert wrapped.__name__ == "load"
    assert wrapped.__doc__ == "Load a file."
    assert wrapped("data.csv") == "data.csv"


def test_background_span_outliving_its_parent_is_still_recorded() -> None:
    release = threading.Event()
    finished = threading.Event()

    def job() -> None:
        release.wait(timeout=5)
        with trace("upload"):
            pass
        finished.set()

    with trace("request") as request:
        thread = threading.Thread(target=propagate_context(job))
        thread.start()

    assert request in finished_spans()
    release.set()
    finished.wait(timeout=5)
    thread.join()

    upload = next(span for span in finished_spans() if span.name == "upload")
    assert upload.parent_id == request.span_id


def test_asyncio_background_task_is_parented_to_its_creator() -> None:
    async def job() -> None:
        async with trace("sync-up"):
            await asyncio.sleep(0)

    async def main() -> None:
        async with trace("run") as run:
            task = asyncio.create_task(job())
            assert current_span() is run
            await task
            child = next(span for span in finished_spans() if span.name == "sync-up")
            assert child.parent_id == run.span_id

    asyncio.run(main())
    assert running_spans() == []
