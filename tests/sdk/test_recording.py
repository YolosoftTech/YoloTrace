import pytest

from yolotrace.sdk import current_span, finished_spans, trace


def test_llm_span_records_kind_and_metadata_known_before_the_call() -> None:
    with trace("chat", kind="llm", attributes={"model": "gpt-x", "temperature": 0.2}):
        pass

    (span,) = finished_spans()
    assert span.kind == "llm"
    assert span.attributes == {"model": "gpt-x", "temperature": 0.2}


def test_values_known_after_the_call_are_recorded_on_the_active_span() -> None:
    @trace("chat", kind="llm", attributes={"model": "gpt-x"})
    def complete(prompt: str) -> str:
        span = current_span()
        assert span is not None
        span.set_attribute("input_tokens", len(prompt))
        return "reply"

    assert complete("hello") == "reply"
    (span,) = finished_spans()
    assert span.attributes == {"model": "gpt-x", "input_tokens": 5}


def test_tool_span_records_kind_and_arguments() -> None:
    @trace("search", kind="tool", attributes={"query": "weather"})
    def search(query: str) -> list[str]:
        return []

    search("weather")
    (span,) = finished_spans()
    assert span.kind == "tool"
    assert span.attributes == {"query": "weather"}


def test_default_kind_is_step() -> None:
    with trace("plan"):
        pass

    (span,) = finished_spans()
    assert span.kind == "step"
    assert span.attributes == {}


def test_run_span_holds_run_metadata() -> None:
    with trace(
        "agent-run", kind="run", attributes={"environment": "dev", "release": "1.2.3"}
    ):
        pass

    (span,) = finished_spans()
    assert span.kind == "run"
    assert span.attributes == {"environment": "dev", "release": "1.2.3"}


def test_mixed_kinds_nest_with_parent_links() -> None:
    with trace("run", kind="run") as run:
        with trace("plan", kind="step") as step, trace("llm", kind="llm") as llm:
            pass
        with trace("lookup", kind="tool") as tool:
            pass

    assert step.parent_id == run.span_id
    assert llm.parent_id == step.span_id
    assert tool.parent_id == run.span_id
    assert [span.kind for span in finished_spans()] == ["llm", "step", "tool", "run"]


def test_failed_llm_call_keeps_kind_and_error() -> None:
    @trace("chat", kind="llm", attributes={"model": "gpt-x"})
    def complete() -> None:
        raise TimeoutError("provider timed out")

    with pytest.raises(TimeoutError):
        complete()

    (span,) = finished_spans()
    assert span.kind == "llm"
    assert span.status == "error"
    assert span.error == "TimeoutError: provider timed out"
    assert span.attributes == {"model": "gpt-x"}


def test_attributes_are_not_shared_between_calls() -> None:
    @trace("chat", kind="llm", attributes={"model": "gpt-x"})
    def complete(tag: str) -> None:
        span = current_span()
        assert span is not None
        span.set_attribute("tag", tag)

    complete("first")
    complete("second")

    first, second = finished_spans()
    assert first.attributes == {"model": "gpt-x", "tag": "first"}
    assert second.attributes == {"model": "gpt-x", "tag": "second"}


def test_caller_attribute_dict_is_copied() -> None:
    attributes = {"model": "gpt-x"}
    with trace("chat", kind="llm", attributes=attributes) as span:
        span.set_attribute("input_tokens", 3)

    assert attributes == {"model": "gpt-x"}


def test_current_span_is_none_outside_a_trace() -> None:
    assert current_span() is None
    with trace("step") as span:
        assert current_span() is span
    assert current_span() is None


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown span kind: 'database'"):
        trace("query", kind="database")  # type: ignore[arg-type]
