import dataclasses

import pytest

from yolotrace.sdk import SpanEvent, configure, current_span, finished_spans, trace


def test_new_span_has_no_events() -> None:
    with trace("block") as span:
        pass

    assert span.events == []
    assert finished_spans()[0].events == []


def test_add_event_records_name_time_and_attributes() -> None:
    with trace("llm") as span:
        span.add_event("retry", {"attempt": 2, "reason": "rate_limited"})

    (event,) = span.events
    assert isinstance(event, SpanEvent)
    assert event.name == "retry"
    assert event.attributes == {"attempt": 2, "reason": "rate_limited"}
    assert span.start_time <= event.time <= (span.end_time or event.time)


def test_events_keep_the_order_they_were_added() -> None:
    with trace("run") as span:
        span.add_event("first")
        span.add_event("second")
        span.add_event("third")

    assert [event.name for event in span.events] == ["first", "second", "third"]
    times = [event.time for event in span.events]
    assert times == sorted(times)


def test_event_without_attributes_has_empty_attributes() -> None:
    with trace("block") as block:
        block.add_event("checkpoint")

    (event,) = block.events
    assert event.attributes == {}


def test_event_can_be_added_to_current_span_from_inside_nested_code() -> None:
    with trace("run") as run, trace("step"):
        span = current_span()
        assert span is not None
        span.add_event("inside_step")

    assert run.events == []
    step = next(span for span in finished_spans() if span.name == "step")
    assert [event.name for event in step.events] == ["inside_step"]


def test_event_on_running_span_is_visible_in_finished_span() -> None:
    with trace("job") as job:
        job.add_event("started")
        assert len(job.events) == 1

    (recorded,) = finished_spans()
    assert recorded is job
    assert [event.name for event in recorded.events] == ["started"]


def test_event_is_immutable() -> None:
    event = SpanEvent(name="frozen", time=1.0)

    with pytest.raises(dataclasses.FrozenInstanceError):
        event.name = "changed"  # type: ignore[misc]


def test_event_attributes_respect_max_attributes() -> None:
    configure(max_attributes=2)

    with trace("block") as span:
        span.add_event("many", {"a": 1, "b": 2, "c": 3})

    (event,) = span.events
    assert event.attributes == {"a": 1, "b": 2}


def test_event_string_attributes_respect_max_attribute_length() -> None:
    configure(max_attribute_length=4)

    with trace("block") as span:
        span.add_event("long", {"text": "abcdefgh"})

    (event,) = span.events
    assert event.attributes == {"text": "abcd"}


def test_event_attributes_do_not_change_span_attributes() -> None:
    with trace("block", attributes={"owner": "team"}) as span:
        span.add_event("note", {"detail": "x"})

    assert span.attributes == {"owner": "team"}


def test_set_attribute_still_respects_max_attributes() -> None:
    configure(max_attributes=1)

    with trace("block") as span:
        span.set_attribute("kept", 1)
        span.set_attribute("dropped", 2)
        span.set_attribute("kept", 3)

    assert span.attributes == {"kept": 3}


def test_error_span_keeps_events_recorded_before_failure() -> None:
    with pytest.raises(RuntimeError), trace("block") as span:
        span.add_event("before_failure")
        raise RuntimeError("boom")

    assert span.status == "error"
    assert [event.name for event in span.events] == ["before_failure"]
