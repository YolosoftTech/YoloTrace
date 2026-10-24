import asyncio
import sys
from typing import Any

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool

from yolotrace.sdk import finished_spans, instrument_langchain, trace


class _FailingChatModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "failing"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise RuntimeError("bad input")


@tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@tool
def explode(text: str) -> str:
    """Always fails."""
    raise ValueError("boom")


def _usage_model() -> GenericFakeChatModel:
    message = AIMessage(
        content="hi",
        usage_metadata={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
    )
    return GenericFakeChatModel(messages=iter([message]))


def _add_one(x: int) -> int:
    return x + 1


def _double(x: int) -> int:
    return x * 2


def test_chain_run_is_recorded_as_run_span_with_nested_steps() -> None:
    instrument_langchain()
    first = RunnableLambda(_add_one).with_config(run_name="first")
    second = RunnableLambda(_double).with_config(run_name="second")

    assert (first | second).invoke(1) == 4

    spans = {span.name: span for span in finished_spans()}
    run = spans["RunnableSequence"]
    assert run.kind == "run"
    assert run.parent_id is None
    assert spans["first"].kind == "step"
    assert spans["first"].parent_id == run.span_id
    assert spans["second"].kind == "step"
    assert spans["second"].parent_id == run.span_id


def test_tool_call_is_recorded_as_tool_span() -> None:
    instrument_langchain()

    assert add.invoke({"a": 1, "b": 2}) == 3

    (span,) = finished_spans()
    assert span.name == "add"
    assert span.kind == "tool"
    assert span.status == "ok"
    assert span.duration is not None
    assert span.attributes == {"tool_name": "add"}


def test_failed_tool_call_is_recorded_and_reraised() -> None:
    instrument_langchain()

    with pytest.raises(ValueError, match="boom"):
        explode.invoke({"text": "x"})

    (span,) = finished_spans()
    assert span.kind == "tool"
    assert span.status == "error"
    assert span.error == "ValueError: boom"


def test_chat_model_call_is_recorded_as_llm_span_with_usage() -> None:
    instrument_langchain()

    response = _usage_model().invoke("hello")

    assert response.content == "hi"
    (span,) = finished_spans()
    assert span.kind == "llm"
    assert span.status == "ok"
    assert span.attributes == {"model": None, "input_tokens": 5, "output_tokens": 7}


def test_failed_chat_model_call_is_recorded_and_reraised() -> None:
    instrument_langchain()

    with pytest.raises(RuntimeError, match="bad input"):
        _FailingChatModel().invoke("hello")

    (span,) = finished_spans()
    assert span.kind == "llm"
    assert span.status == "error"
    assert span.error == "RuntimeError: bad input"
    assert "input_tokens" not in span.attributes


def test_framework_span_is_parented_to_enclosing_trace() -> None:
    instrument_langchain()

    with trace("agent-run") as run:
        add.invoke({"a": 1, "b": 2})

    tool_span = next(s for s in finished_spans() if s.kind == "tool")
    assert tool_span.parent_id == run.span_id


def test_instrumenting_twice_records_each_call_once() -> None:
    instrument_langchain()
    instrument_langchain()

    add.invoke({"a": 1, "b": 2})

    assert len(finished_spans()) == 1


def test_missing_langchain_package_gives_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(
        sys.modules, "yolotrace.sdk.langchain_integration", raising=False
    )
    monkeypatch.setitem(sys.modules, "langchain_core.callbacks", None)

    with pytest.raises(ImportError, match="requires the 'langchain-core' package"):
        instrument_langchain()


def test_async_tool_call_is_recorded_as_tool_span() -> None:
    instrument_langchain()

    assert asyncio.run(add.ainvoke({"a": 1, "b": 2})) == 3

    (span,) = finished_spans()
    assert span.kind == "tool"
    assert span.status == "ok"
    assert span.attributes == {"tool_name": "add"}
