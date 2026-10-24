import asyncio
from collections.abc import Iterator

import httpx2
import pytest
from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from openai import AsyncOpenAI, OpenAI
from openai.resources.chat.completions import AsyncCompletions, Completions

from yolotrace.sdk import (
    finished_spans,
    instrument_langchain,
    instrument_openai,
    instrument_openai_agents,
)
from yolotrace.sdk.llm_calls import Provider, wrap_create

CHAT_OK = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 0,
    "model": "gpt-test",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hi"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
}


@pytest.fixture(autouse=True)
def restore_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the wrapping that instrumenting the OpenAI clients applies, after each test."""
    monkeypatch.setattr(Completions, "create", Completions.create)
    monkeypatch.setattr(AsyncCompletions, "create", AsyncCompletions.create)


@pytest.fixture(autouse=True)
def no_sdk_export(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without a key the Agents SDK's own exporter skips sending, so tests stay offline.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def _handler(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json=CHAT_OK)


def _sync_client() -> OpenAI:
    http_client = httpx2.Client(transport=httpx2.MockTransport(_handler))
    return OpenAI(api_key="test", http_client=http_client, max_retries=0)


def _agents_model() -> OpenAIChatCompletionsModel:
    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(_handler))
    client = AsyncOpenAI(api_key="test", http_client=http_client, max_retries=0)
    return OpenAIChatCompletionsModel(model="gpt-test", openai_client=client)


def _llm_spans() -> list[str]:
    return [span.name for span in finished_spans() if span.kind == "llm"]


def test_agents_model_call_is_recorded_once_when_openai_is_also_instrumented() -> None:
    instrument_openai()
    instrument_openai_agents()
    agent = Agent(name="plain", model=_agents_model())

    Runner.run_sync(agent, "hello")

    assert _llm_spans() == ["openai_agents.generation"]


def test_langchain_model_call_is_recorded_once_when_openai_is_also_instrumented() -> (
    None
):
    client = _sync_client()

    def replies() -> Iterator[AIMessage]:
        client.chat.completions.create(
            model="gpt-test", messages=[{"role": "user", "content": "hi"}]
        )
        yield AIMessage(content="hi")

    instrument_openai()
    instrument_langchain()

    GenericFakeChatModel(messages=replies()).invoke("hello")

    assert _llm_spans() == ["llm"]


def test_provider_call_nested_in_a_recorded_call_is_not_recorded_twice() -> None:
    provider = Provider(
        name="test",
        span_name="test.create",
        read_tokens=lambda response: (None, None),
    )

    def inner_create(*args: object, **kwargs: object) -> object:
        return None

    inner = wrap_create(inner_create, provider)

    def outer_create(*args: object, **kwargs: object) -> object:
        return inner(*args, **kwargs)

    wrap_create(outer_create, provider)(model="test-model")

    assert _llm_spans() == ["test.create"]


def test_provider_call_outside_any_recorded_call_is_still_recorded() -> None:
    instrument_openai()
    client: OpenAI = _sync_client()

    client.chat.completions.create(
        model="gpt-test", messages=[{"role": "user", "content": "hi"}]
    )
    client.chat.completions.create(
        model="gpt-test", messages=[{"role": "user", "content": "again"}]
    )

    assert _llm_spans() == [
        "openai.chat.completions.create",
        "openai.chat.completions.create",
    ]


def test_async_langchain_model_call_is_recorded_once_when_openai_is_also_instrumented() -> (
    None
):
    client = _sync_client()

    def replies() -> Iterator[AIMessage]:
        client.chat.completions.create(
            model="gpt-test", messages=[{"role": "user", "content": "hi"}]
        )
        yield AIMessage(content="hi")

    instrument_openai()
    instrument_langchain()

    asyncio.run(GenericFakeChatModel(messages=replies()).ainvoke("hello"))

    assert _llm_spans() == ["llm"]
