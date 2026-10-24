import pytest

from yolotrace.sdk import (
    SpanContext,
    configure,
    continue_trace,
    extract,
    finished_spans,
    inject,
    running_spans,
    start_span,
    trace,
)


def test_everything_is_recorded_by_default() -> None:
    with trace("outer"), trace("inner"):
        pass

    assert [span.name for span in finished_spans()] == ["inner", "outer"]


def test_zero_sample_rate_records_nothing_but_code_still_runs() -> None:
    configure(sample_rate=0.0)

    @trace()
    def add(a: int, b: int) -> int:
        return a + b

    assert add(2, 3) == 5
    with trace("block") as block:
        block.set_attribute("key", "value")
    job = start_span("job")
    assert running_spans() == []
    job.end()

    assert finished_spans() == []


def test_children_follow_the_sampling_decision_of_their_root() -> None:
    configure(sample_rate=0.0)

    with trace("root"):
        # Changing the rate mid-trace must not record a child of an unsampled root.
        configure(sample_rate=1.0)
        with trace("child"):
            pass
        assert running_spans() == []

    assert finished_spans() == []


def test_sampled_root_records_children_even_if_rate_changes_later() -> None:
    with trace("root"):
        configure(sample_rate=0.0)
        with trace("child"):
            pass

    assert [span.name for span in finished_spans()] == ["child", "root"]


def test_sample_rate_outside_zero_to_one_is_rejected() -> None:
    with pytest.raises(ValueError, match="sample_rate"):
        configure(sample_rate=-0.1)
    with pytest.raises(ValueError, match="sample_rate"):
        configure(sample_rate=1.1)


def test_negative_limits_are_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        configure(max_attributes=-1)
    with pytest.raises(ValueError, match="cannot be negative"):
        configure(max_attribute_length=-1)


def test_configure_without_arguments_resets_to_defaults() -> None:
    configure(sample_rate=0.0, max_attributes=0, max_attribute_length=1)
    configure()

    with trace("block", attributes={"a": "abc"}) as span:
        pass

    assert span.attributes == {"a": "abc"}
    assert len(finished_spans()) == 1


def test_inject_marks_unsampled_context_in_traceparent() -> None:
    configure(sample_rate=0.0)
    carrier: dict[str, str] = {}

    with trace("caller") as caller:
        inject(carrier)

    assert carrier == {"traceparent": f"00-{caller.trace_id}-{caller.span_id}-00"}


def test_extract_reads_the_sampled_flag() -> None:
    sampled = {"traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"}
    unsampled = {"traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-00"}

    assert extract(sampled) == SpanContext(
        trace_id="a" * 32, span_id="b" * 16, sampled=True
    )
    assert extract(unsampled) == SpanContext(
        trace_id="a" * 32, span_id="b" * 16, sampled=False
    )


def test_unsampled_trace_stays_unsampled_in_another_process() -> None:
    configure(sample_rate=0.0)
    carrier: dict[str, str] = {}
    with trace("caller"):
        inject(carrier)

    configure(sample_rate=1.0)
    with continue_trace(extract(carrier)), trace("job"):
        pass

    assert finished_spans() == []


def test_sampled_trace_is_recorded_in_another_process_regardless_of_local_rate() -> (
    None
):
    configure(sample_rate=1.0)
    carrier: dict[str, str] = {}
    with trace("caller"):
        inject(carrier)

    configure(sample_rate=0.0)
    with continue_trace(extract(carrier)), trace("job") as job:
        pass

    assert [span.name for span in finished_spans()] == ["caller", "job"]
    assert finished_spans()[-1] is job


def test_attributes_beyond_the_limit_are_dropped() -> None:
    configure(max_attributes=2)

    with trace("block", attributes={"a": 1, "b": 2, "c": 3}) as span:
        span.set_attribute("d", 4)
        span.set_attribute("a", 10)

    assert span.attributes == {"a": 10, "b": 2}


def test_string_attributes_are_cut_to_the_limit() -> None:
    configure(max_attribute_length=4)

    with trace("block", attributes={"text": "abcdefgh", "count": 123456}) as span:
        span.set_attribute("late", "xyzxyz")

    assert span.attributes == {"text": "abcd", "count": 123456, "late": "xyzx"}


def test_error_message_is_cut_to_the_limit() -> None:
    configure(max_attribute_length=10)

    with pytest.raises(RuntimeError), trace("block") as span:
        raise RuntimeError("a very long failure message")

    assert span.status == "error"
    assert span.error == "RuntimeErr"
