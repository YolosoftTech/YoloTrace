import pytest

from yolotrace.sdk import finished_spans, trace


def test_decorator_records_span_named_after_function() -> None:
    @trace()
    def add(a: int, b: int) -> int:
        return a + b

    assert add(2, 3) == 5
    (span,) = finished_spans()
    assert span.name == add.__qualname__
    assert span.status == "ok"
    assert span.error is None
    assert span.parent_id is None
    assert span.duration is not None and span.duration >= 0


def test_decorator_uses_explicit_name_and_keeps_function_metadata() -> None:
    @trace("custom")
    def greet() -> str:
        """Say hello."""
        return "hi"

    assert greet() == "hi"
    assert greet.__name__ == "greet"
    assert greet.__doc__ == "Say hello."
    assert [span.name for span in finished_spans()] == ["custom"]


def test_context_manager_records_block() -> None:
    with trace("block") as span:
        assert span.name == "block"
        assert span.end_time is None

    (recorded,) = finished_spans()
    assert recorded is span
    assert recorded.end_time is not None and recorded.end_time >= recorded.start_time


def test_context_manager_requires_name() -> None:
    with pytest.raises(ValueError, match="span name is required"), trace():
        pass


def test_nested_spans_link_to_parent() -> None:
    with trace("run") as run:
        with trace("llm_call") as llm:
            pass
        with trace("tool_call") as tool:
            pass

    assert llm.parent_id == run.span_id
    assert tool.parent_id == run.span_id
    assert run.parent_id is None
    assert [span.name for span in finished_spans()] == ["llm_call", "tool_call", "run"]


def test_recursive_decorated_function_builds_nested_spans() -> None:
    @trace("countdown")
    def countdown(n: int) -> int:
        return 0 if n == 0 else countdown(n - 1)

    assert countdown(2) == 0
    spans = finished_spans()
    assert len(spans) == 3
    assert spans[0].parent_id == spans[1].span_id
    assert spans[1].parent_id == spans[2].span_id
    assert spans[2].parent_id is None


def test_exception_is_recorded_and_propagated() -> None:
    @trace()
    def fail() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        fail()

    (span,) = finished_spans()
    assert span.status == "error"
    assert span.error == "RuntimeError: boom"
    assert span.end_time is not None


def test_failed_child_does_not_leak_into_following_spans() -> None:
    with trace("run") as run:
        with pytest.raises(ValueError), trace("broken"):
            raise ValueError("bad input")
        with trace("after") as after:
            pass

    assert after.parent_id == run.span_id
    spans = {span.name: span for span in finished_spans()}
    assert spans["broken"].status == "error"
    assert spans["after"].status == "ok"
    assert spans["run"].status == "ok"


def test_exception_in_block_is_recorded_and_propagated() -> None:
    with pytest.raises(KeyError), trace("lookup"):
        raise KeyError("missing")

    (span,) = finished_spans()
    assert span.status == "error"
    assert span.error == "KeyError: 'missing'"
