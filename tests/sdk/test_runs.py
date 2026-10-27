import asyncio
import dataclasses
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from yolotrace.sdk import (
    Run,
    Span,
    SpanContext,
    configure,
    continue_trace,
    extract,
    finished_spans,
    inject,
    propagate_context,
    runs,
    start_span,
    trace,
)


def test_no_finished_spans_gives_no_runs() -> None:
    assert runs() == []


def test_nested_spans_form_one_run_with_linked_children() -> None:
    with trace("answer", kind="run") as root:
        with trace("lookup") as lookup, trace("fetch") as fetch:
            pass
        with trace("format") as formatted:
            pass

    (run,) = runs()
    assert isinstance(run, Run)
    assert run.trace_id == root.trace_id
    assert run.roots == (root,)
    assert run.children(root) == (lookup, formatted)
    assert run.children(lookup) == (fetch,)
    assert run.children(fetch) == ()
    assert run.children(formatted) == ()


def test_separate_traces_form_separate_runs() -> None:
    with trace("first") as first:
        pass
    with trace("second") as second:
        pass

    first_run, second_run = runs()
    assert first_run.trace_id == first.trace_id
    assert second_run.trace_id == second.trace_id
    assert first_run.spans == (first,)
    assert second_run.spans == (second,)


def test_run_lists_spans_in_start_order() -> None:
    with trace("root") as root:
        time.sleep(0.01)
        with trace("early"):
            time.sleep(0.01)
        time.sleep(0.01)
        with trace("late") as late:
            time.sleep(0.01)

    (run,) = runs()
    starts = [span.start_time for span in run.spans]
    assert starts == sorted(starts)
    assert run.spans[0] is root
    assert run.spans[-1] is late


def test_runs_are_ordered_by_their_first_span() -> None:
    with trace("older"):
        time.sleep(0.01)
    with trace("newer"):
        pass

    names = [run.spans[0].name for run in runs()]
    assert names == ["older", "newer"]


def test_multiple_roots_in_one_trace_belong_to_the_same_run() -> None:
    remote = SpanContext(trace_id="c" * 32, span_id="d" * 16)

    with continue_trace(remote), trace("first") as first:
        pass
    with continue_trace(remote), trace("second") as second:
        pass

    (run,) = runs()
    assert run.trace_id == "c" * 32
    assert run.roots == (first, second)


def test_span_whose_parent_was_recorded_elsewhere_is_a_root() -> None:
    # The caller process recorded the parent span, so this process never sees it.
    carrier: dict[str, str] = {"traceparent": f"00-{'a' * 32}-{'b' * 16}-01"}
    with continue_trace(extract(carrier)), trace("job") as job:
        pass

    (run,) = runs()
    assert run.trace_id == "a" * 32
    assert job.parent_id == "b" * 16
    assert run.roots == (job,)


def test_span_whose_parent_is_recorded_here_is_not_a_root() -> None:
    with trace("request") as request:
        carrier: dict[str, str] = {}
        inject(carrier)
    with continue_trace(extract(carrier)), trace("job") as job:
        pass

    (run,) = runs()
    assert run.trace_id == request.trace_id
    assert run.roots == (request,)
    assert run.children(request) == (job,)


def test_child_of_running_parent_is_a_root_until_the_parent_ends() -> None:
    parent = start_span("parent")
    parent_context = SpanContext(trace_id=parent.trace_id, span_id=parent.span_id)
    with continue_trace(parent_context), trace("child") as child:
        pass

    (run,) = runs()
    assert run.roots == (child,)

    parent.end()

    (run,) = runs()
    assert run.roots == (parent,)
    assert run.children(parent) == (child,)


def test_running_spans_are_not_part_of_any_run() -> None:
    with trace("request") as request:
        pending = start_span("upload")

    (run,) = runs()
    assert run.spans == (request,)

    pending.end()

    (run,) = runs()
    assert run.spans == (request, pending)
    assert run.children(request) == (pending,)


def test_unsampled_traces_are_not_grouped() -> None:
    configure(sample_rate=0.0)

    with trace("hidden"):
        pass

    assert finished_spans() == []
    assert runs() == []


def test_failed_span_stays_in_its_run_with_its_parent_link() -> None:
    with (
        pytest.raises(RuntimeError),
        trace("run", kind="run") as root,
        trace("fail") as failed,
    ):
        raise RuntimeError("boom")

    (run,) = runs()
    assert run.children(root) == (failed,)
    assert failed.status == "error"


def test_thread_with_propagated_context_joins_the_run() -> None:
    recorded: list[Span] = []

    def job() -> None:
        with trace("job") as job_span:
            recorded.append(job_span)

    with trace("batch") as batch, ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(propagate_context(job)).result()

    (run,) = runs()
    assert run.children(batch) == (recorded[0],)


async def test_concurrent_async_tasks_are_children_of_their_run() -> None:
    async def worker(name: str) -> None:
        with trace(name):
            await asyncio.sleep(0)

    with trace("batch") as batch:
        await asyncio.gather(worker("a"), worker("b"))

    (run,) = runs()
    assert {child.name for child in run.children(batch)} == {"a", "b"}
    assert all(span.trace_id == batch.trace_id for span in run.spans)


def test_run_is_immutable() -> None:
    with trace("block"):
        pass
    (run,) = runs()

    with pytest.raises(dataclasses.FrozenInstanceError):
        run.trace_id = "changed"  # type: ignore[misc]
