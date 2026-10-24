import pytest

from yolotrace.sdk import Span, current_span, finished_spans, start_span, trace


def test_manual_span_is_recorded_only_when_ended() -> None:
    span = start_span("job")
    assert span.name == "job"
    assert span.end_time is None
    assert finished_spans() == []

    span.end()

    assert finished_spans() == [span]
    assert span.status == "ok"
    assert span.duration is not None and span.duration >= 0


def test_span_can_start_and_end_in_different_functions() -> None:
    def begin() -> Span:
        return start_span("upload", kind="tool", attributes={"bytes": 10})

    def finish(span: Span) -> None:
        span.end()

    span = begin()
    finish(span)

    (recorded,) = finished_spans()
    assert recorded is span
    assert recorded.kind == "tool"
    assert recorded.attributes == {"bytes": 10}


def test_ending_with_exception_marks_span_as_error() -> None:
    span = start_span("upload")
    span.end(TimeoutError("slow network"))

    (recorded,) = finished_spans()
    assert recorded.status == "error"
    assert recorded.error == "TimeoutError: slow network"
    assert recorded.end_time is not None


def test_attributes_can_be_set_before_ending() -> None:
    span = start_span("chat", kind="llm", attributes={"model": "gpt-x"})
    span.set_attribute("output_tokens", 42)
    span.end()

    (recorded,) = finished_spans()
    assert recorded.kind == "llm"
    assert recorded.attributes == {"model": "gpt-x", "output_tokens": 42}


def test_manual_span_is_parented_to_current_span_at_start() -> None:
    with trace("run") as run:
        span = start_span("background")
    span.end()

    assert span.parent_id == run.span_id


def test_manual_span_does_not_become_current_span() -> None:
    span = start_span("job")
    assert current_span() is None

    with trace("block") as block:
        assert block.parent_id is None
    span.end()


def test_nested_trace_does_not_link_to_manual_span() -> None:
    manual = start_span("job")
    with trace("block") as block:
        pass
    manual.end()

    assert block.parent_id is None


def test_ending_twice_is_rejected_and_not_recorded_twice() -> None:
    span = start_span("job")
    span.end()

    with pytest.raises(RuntimeError, match="'job' has already ended"):
        span.end()

    assert finished_spans() == [span]


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown span kind: 'database'"):
        start_span("query", kind="database")  # type: ignore[arg-type]


def test_caller_attribute_dict_is_copied() -> None:
    attributes = {"model": "gpt-x"}
    span = start_span("chat", kind="llm", attributes=attributes)
    span.set_attribute("input_tokens", 3)
    span.end()

    assert attributes == {"model": "gpt-x"}
