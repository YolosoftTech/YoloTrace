from yolotrace.ingestion.store import TraceStore
from yolotrace.sdk.tracing import Span

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
ROOT_ID = "1111111111111111"
CHILD_ID = "2222222222222222"


def _span(
    name: str,
    span_id: str,
    *,
    parent_id: str | None = None,
    end_time: float | None = 2.0,
    trace_id: str = TRACE_ID,
) -> Span:
    """Return a span that started at 1.0 and ended at ``end_time``, or is still running when it is ``None``."""
    return Span(
        name=name,
        trace_id=trace_id,
        span_id=span_id,
        parent_id=parent_id,
        start_time=1.0,
        end_time=end_time,
    )


def test_child_stored_before_its_parent_is_kept_and_the_parent_follows() -> None:
    store = TraceStore()

    store.add([_span("child", CHILD_ID, parent_id=ROOT_ID)])
    store.add([_span("root", ROOT_ID)])

    assert [span.name for span in store.spans()] == ["child", "root"]


def test_running_root_with_finished_children_is_stored_as_a_partial_run() -> None:
    store = TraceStore()

    store.add([_span("root", ROOT_ID, end_time=None)])
    store.add([_span("child", CHILD_ID, parent_id=ROOT_ID)])

    root, child = store.spans()
    assert root.end_time is None
    assert child.parent_id == root.span_id


def test_running_span_is_replaced_by_its_finished_form() -> None:
    store = TraceStore()

    store.add([_span("root", ROOT_ID, end_time=None)])
    store.add([_span("root", ROOT_ID, end_time=3.0)])

    [span] = store.spans()
    assert span.end_time == 3.0


def test_running_span_is_replaced_by_its_newer_running_snapshot() -> None:
    store = TraceStore()
    first = _span("root", ROOT_ID, end_time=None)
    later = _span("root", ROOT_ID, end_time=None)
    later.add_event("checkpoint")

    store.add([first])
    store.add([later])

    [span] = store.spans()
    assert span is later


def test_finished_span_is_not_replaced_by_a_late_running_copy() -> None:
    store = TraceStore()
    finished = _span("root", ROOT_ID, end_time=2.0)

    store.add([finished])
    store.add([_span("root", ROOT_ID, end_time=None)])

    assert store.spans() == [finished]


def test_retried_finished_span_is_stored_once_and_the_first_copy_is_kept() -> None:
    store = TraceStore()
    first = _span("root", ROOT_ID, end_time=2.0)

    store.add([first])
    store.add([_span("root", ROOT_ID, end_time=2.0)])

    assert store.spans() == [first]


def test_span_repeated_within_one_batch_keeps_its_finished_form() -> None:
    store = TraceStore()
    finished = _span("root", ROOT_ID, end_time=2.0)

    store.add([_span("root", ROOT_ID, end_time=None), finished])

    assert store.spans() == [finished]


def test_same_span_id_in_different_traces_is_stored_separately() -> None:
    store = TraceStore()
    other_trace = "fedcba9876543210fedcba9876543210"

    store.add([_span("root", ROOT_ID)])
    store.add([_span("root", ROOT_ID, trace_id=other_trace)])

    assert [span.trace_id for span in store.spans()] == [TRACE_ID, other_trace]


def test_replacing_a_running_span_keeps_its_original_position() -> None:
    store = TraceStore()

    store.add([_span("root", ROOT_ID, end_time=None), _span("child", CHILD_ID)])
    store.add([_span("root", ROOT_ID, end_time=5.0)])

    assert [span.name for span in store.spans()] == ["root", "child"]


def test_spans_returns_a_copy_that_does_not_change_the_store() -> None:
    store = TraceStore()
    store.add([_span("root", ROOT_ID)])

    store.spans().clear()

    assert len(store.spans()) == 1
