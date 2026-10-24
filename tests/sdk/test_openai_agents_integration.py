import asyncio
import sys
from collections.abc import Callable

import httpx2
import pytest
from agents import Agent, Runner, function_tool
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from yolotrace.sdk import Span, finished_spans, instrument_openai_agents, trace


def _message(
    content: str | None, tool_calls: list[dict[str, object]] | None = None
) -> dict[str, object]:
    message: dict[str, object] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    finish = "tool_calls" if tool_calls else "stop"
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-test",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
    }


def _tool_call(name: str, arguments: str) -> dict[str, object]:
    return {
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _model_with_handler(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> OpenAIChatCompletionsModel:
    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    client = AsyncOpenAI(api_key="test", http_client=http_client, max_retries=0)
    return OpenAIChatCompletionsModel(model="gpt-test", openai_client=client)


def _model(responses: list[dict[str, object]]) -> OpenAIChatCompletionsModel:
    """Chat Completions model that answers each request with the next canned response."""
    queue = iter(responses)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=next(queue))

    return _model_with_handler(handler)


def _failing_model() -> OpenAIChatCompletionsModel:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise RuntimeError("bad input")

    return _model_with_handler(handler)


def _ancestor_ids(span: Span, spans: list[Span]) -> list[str]:
    """Span IDs above ``span``, from its parent up to the root, by following parent links."""
    by_id = {s.span_id: s for s in spans}
    ancestors: list[str] = []
    parent_id = span.parent_id
    while parent_id is not None and parent_id in by_id:
        ancestors.append(parent_id)
        parent_id = by_id[parent_id].parent_id
    return ancestors


@function_tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@pytest.fixture(autouse=True)
def no_sdk_export(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without a key the Agents SDK's own exporter skips sending, so tests stay offline.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_agent_run_is_recorded_as_run_with_agent_model_and_tool_spans() -> None:
    instrument_openai_agents()
    model = _model(
        [
            _message(None, [_tool_call("add", '{"a": 1, "b": 2}')]),
            _message("three"),
        ]
    )
    agent = Agent(name="calculator", model=model, tools=[add])

    result = Runner.run_sync(agent, "what is 1 + 2?")

    assert result.final_output == "three"
    spans = finished_spans()
    run = next(s for s in spans if s.kind == "run")
    agent_span = next(s for s in spans if s.name == "calculator")
    tool_span = next(s for s in spans if s.kind == "tool")
    llm_spans = [s for s in spans if s.kind == "llm"]

    assert run.parent_id is None
    assert agent_span.kind == "step"
    assert run.span_id in _ancestor_ids(agent_span, spans)
    assert tool_span.name == "add"
    assert tool_span.attributes == {"tool_name": "add"}
    assert agent_span.span_id in _ancestor_ids(tool_span, spans)
    assert len(llm_spans) == 2
    assert all(agent_span.span_id in _ancestor_ids(s, spans) for s in llm_spans)
    assert all(s.status == "ok" for s in spans)


def test_model_call_records_token_usage() -> None:
    instrument_openai_agents()
    agent = Agent(name="plain", model=_model([_message("hi")]))

    Runner.run_sync(agent, "hello")

    (llm_span,) = [s for s in finished_spans() if s.kind == "llm"]
    assert llm_span.attributes["input_tokens"] == 5
    assert llm_span.attributes["output_tokens"] == 7


def test_handoff_is_recorded_as_step_between_agents() -> None:
    instrument_openai_agents()
    specialist = Agent(name="specialist", model=_model([_message("done")]))
    triage = Agent(
        name="triage",
        model=_model([_message(None, [_tool_call("transfer_to_specialist", "{}")])]),
        handoffs=[specialist],
    )

    Runner.run_sync(triage, "help me")

    handoff = next(s for s in finished_spans() if s.name == "handoff")
    assert handoff.kind == "step"
    assert handoff.attributes == {"from_agent": "triage", "to_agent": "specialist"}


def test_failed_model_call_is_recorded_as_error() -> None:
    instrument_openai_agents()
    agent = Agent(name="broken", model=_failing_model())

    with pytest.raises(RuntimeError, match="bad input"):
        Runner.run_sync(agent, "hello")

    llm_span = next(s for s in finished_spans() if s.kind == "llm")
    assert llm_span.status == "error"
    assert llm_span.error == "Error getting response"


def test_agent_run_is_parented_to_enclosing_trace() -> None:
    instrument_openai_agents()
    agent = Agent(name="plain", model=_model([_message("hi")]))

    with trace("app-request") as request:
        Runner.run_sync(agent, "hello")

    run = next(s for s in finished_spans() if s.kind == "run")
    assert run.parent_id == request.span_id


def test_instrumenting_twice_records_each_span_once() -> None:
    instrument_openai_agents()
    instrument_openai_agents()
    agent = Agent(name="plain", model=_model([_message("hi")]))

    Runner.run_sync(agent, "hello")

    runs = [s for s in finished_spans() if s.kind == "run"]
    assert len(runs) == 1


def test_missing_agents_package_gives_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(
        sys.modules, "yolotrace.sdk.openai_agents_integration", raising=False
    )
    monkeypatch.setitem(sys.modules, "agents", None)

    with pytest.raises(ImportError, match="requires the 'openai-agents' package"):
        instrument_openai_agents()


def test_async_agent_run_is_recorded_as_run_span() -> None:
    instrument_openai_agents()
    agent = Agent(name="plain", model=_model([_message("hi")]))

    result = asyncio.run(Runner.run(agent, "hello"))

    assert result.final_output == "hi"
    run = next(s for s in finished_spans() if s.kind == "run")
    agent_span = next(s for s in finished_spans() if s.name == "plain")
    assert run.span_id in _ancestor_ids(agent_span, finished_spans())
    assert all(s.status == "ok" for s in finished_spans())
