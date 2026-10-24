import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from yolotrace.sdk import (
    Span,
    SpanContext,
    continue_trace,
    extract,
    finished_spans,
    inject,
    propagate_context,
    start_span,
    trace,
)

HEX_TRACE_ID = re.compile(r"[0-9a-f]{32}")
HEX_SPAN_ID = re.compile(r"[0-9a-f]{16}")


def test_root_spans_get_fresh_trace_ids() -> None:
    with trace("first") as first:
        pass
    with trace("second") as second:
        pass

    assert HEX_TRACE_ID.fullmatch(first.trace_id)
    assert HEX_SPAN_ID.fullmatch(first.span_id)
    assert first.trace_id != second.trace_id


def test_nested_spans_share_the_trace_id() -> None:
    with trace("run") as run:
        with trace("llm") as llm:
            pass
        tool = start_span("tool", kind="tool")
        tool.end()

    assert llm.trace_id == run.trace_id
    assert tool.trace_id == run.trace_id


def test_inject_writes_the_current_span_as_traceparent() -> None:
    carrier: dict[str, str] = {}

    with trace("caller") as caller:
        inject(carrier)

    assert carrier == {"traceparent": f"00-{caller.trace_id}-{caller.span_id}-01"}


def test_inject_writes_nothing_without_an_active_context() -> None:
    carrier: dict[str, str] = {}

    inject(carrier)

    assert carrier == {}


def test_extract_reads_what_inject_wrote() -> None:
    carrier: dict[str, str] = {}

    with trace("caller") as caller:
        inject(carrier)

    assert extract(carrier) == SpanContext(
        trace_id=caller.trace_id, span_id=caller.span_id
    )


@pytest.mark.parametrize(
    "carrier",
    [
        {},
        {"traceparent": ""},
        {"traceparent": "not-a-traceparent"},
        {"traceparent": "01-" + "a" * 32 + "-" + "b" * 16 + "-01"},
        {"traceparent": "00-" + "A" * 32 + "-" + "b" * 16 + "-01"},
        {"traceparent": "00-" + "a" * 32 + "-" + "b" * 15 + "-01"},
        {"traceparent": "00-" + "0" * 32 + "-" + "b" * 16 + "-01"},
        {"traceparent": "00-" + "a" * 32 + "-" + "0" * 16 + "-01"},
        {"Traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"},
    ],
)
def test_extract_returns_none_for_missing_or_malformed_context(
    carrier: dict[str, str],
) -> None:
    assert extract(carrier) is None


def test_continue_trace_parents_new_root_spans_to_the_remote_span() -> None:
    remote = SpanContext(trace_id="a" * 32, span_id="b" * 16)

    with continue_trace(remote), trace("handler") as handler:
        pass

    assert handler.trace_id == remote.trace_id
    assert handler.parent_id == remote.span_id
    assert [span.name for span in finished_spans()] == ["handler"]


def test_continue_trace_restores_the_previous_context_after_the_block() -> None:
    remote = SpanContext(trace_id="a" * 32, span_id="b" * 16)

    with continue_trace(remote):
        pass
    with trace("later") as later:
        pass

    assert later.trace_id != remote.trace_id
    assert later.parent_id is None


def test_continue_trace_with_none_starts_a_new_trace() -> None:
    with continue_trace(None), trace("handler") as handler:
        pass

    assert handler.parent_id is None
    assert HEX_TRACE_ID.fullmatch(handler.trace_id)


def test_local_span_takes_precedence_over_remote_context() -> None:
    remote = SpanContext(trace_id="a" * 32, span_id="b" * 16)
    carrier: dict[str, str] = {}

    with continue_trace(remote), trace("local") as local:
        inject(carrier)
        with trace("inner") as inner:
            pass

    assert inner.parent_id == local.span_id
    assert inner.trace_id == remote.trace_id
    assert extract(carrier) == SpanContext(
        trace_id=remote.trace_id, span_id=local.span_id
    )


def test_inject_inside_remote_context_forwards_it_unchanged() -> None:
    remote = SpanContext(trace_id="a" * 32, span_id="b" * 16)
    carrier: dict[str, str] = {}

    with continue_trace(remote):
        inject(carrier)

    assert extract(carrier) == remote


def test_propagated_thread_work_keeps_the_trace_id() -> None:
    with trace("batch") as batch, ThreadPoolExecutor() as pool:
        worker = pool.submit(propagate_context(_record_worker)).result()

    assert worker.trace_id == batch.trace_id
    assert worker.parent_id == batch.span_id


def _record_worker() -> Span:
    with trace("worker") as worker:
        pass
    return worker


def test_trace_continues_in_another_process() -> None:
    script = (
        "import json, sys\n"
        "from yolotrace import continue_trace, extract, trace\n"
        "context = extract(json.loads(sys.argv[1]))\n"
        "with continue_trace(context), trace('callee') as span:\n"
        "    pass\n"
        "print(json.dumps({'trace_id': span.trace_id, 'parent_id': span.parent_id}))\n"
    )
    carrier: dict[str, str] = {}

    with trace("caller") as caller:
        inject(carrier)

    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps(carrier)],
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(result.stdout) == {
        "trace_id": caller.trace_id,
        "parent_id": caller.span_id,
    }
