import sys
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from anthropic import Anthropic
from anthropic.resources.messages import Messages
from anthropic.types import Message
from openai import OpenAI
from openai.resources.chat.completions import Completions

from yolotrace.sdk import finished_spans, instrument_anthropic, instrument_openai, trace

OPENAI_OK = {
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

ANTHROPIC_OK = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-test",
    "content": [{"type": "text", "text": "hi"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 5, "output_tokens": 7},
}


@pytest.fixture(autouse=True)
def restore_clients() -> Iterator[None]:
    completions_create = Completions.create
    messages_create = Messages.create
    yield
    Completions.create = completions_create  # type: ignore[method-assign]
    Messages.create = messages_create  # type: ignore[method-assign]


def _openai_client(status: int, body: dict[str, Any]) -> OpenAI:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAI(api_key="test", http_client=http_client, max_retries=0)


def _anthropic_messages(response: Message | None = None) -> Messages:
    """Messages resource whose transport returns ``response``, or raises if it is None."""

    def post(*args: Any, **kwargs: Any) -> Message:
        if response is None:
            raise RuntimeError("bad input")
        return response

    client = Anthropic(api_key="test", max_retries=0)
    client.post = post  # type: ignore[method-assign]
    return Messages(client)


def _openai_chat(client: OpenAI) -> Any:
    return client.chat.completions.create(
        model="gpt-test",
        messages=[{"role": "user", "content": "hello"}],
    )


def _anthropic_message(messages: Messages) -> Any:
    return messages.create(
        model="claude-test",
        max_tokens=100,
        messages=[{"role": "user", "content": "hello"}],
    )


def test_openai_call_is_recorded_as_llm_span() -> None:
    client = _openai_client(200, OPENAI_OK)
    instrument_openai()

    response = _openai_chat(client)

    assert response.choices[0].message.content == "hi"
    (span,) = finished_spans()
    assert span.name == "openai.chat.completions.create"
    assert span.kind == "llm"
    assert span.status == "ok"
    assert span.duration is not None
    assert span.attributes == {
        "provider": "openai",
        "model": "gpt-test",
        "input_tokens": 5,
        "output_tokens": 7,
    }


def test_anthropic_call_is_recorded_as_llm_span() -> None:
    messages = _anthropic_messages(Message.model_validate(ANTHROPIC_OK))
    instrument_anthropic()

    response = _anthropic_message(messages)

    assert response.content[0].text == "hi"  # type: ignore[union-attr]
    (span,) = finished_spans()
    assert span.name == "anthropic.messages.create"
    assert span.kind == "llm"
    assert span.status == "ok"
    assert span.attributes == {
        "provider": "anthropic",
        "model": "claude-test",
        "input_tokens": 5,
        "output_tokens": 7,
    }


def test_failed_openai_call_is_recorded_and_reraised() -> None:
    client = _openai_client(400, {"error": {"message": "bad input", "type": "x"}})
    instrument_openai()

    with pytest.raises(Exception, match="bad input"):
        _openai_chat(client)

    (span,) = finished_spans()
    assert span.status == "error"
    assert span.error is not None and "bad input" in span.error
    assert "input_tokens" not in span.attributes


def test_failed_anthropic_call_is_recorded_and_reraised() -> None:
    messages = _anthropic_messages(None)
    instrument_anthropic()

    with pytest.raises(RuntimeError, match="bad input"):
        _anthropic_message(messages)

    (span,) = finished_spans()
    assert span.status == "error"
    assert span.error == "RuntimeError: bad input"


def test_calls_are_not_recorded_before_instrumenting() -> None:
    _openai_chat(_openai_client(200, OPENAI_OK))

    assert finished_spans() == []


def test_instrumenting_twice_records_each_call_once() -> None:
    client = _openai_client(200, OPENAI_OK)
    instrument_openai()
    instrument_openai()

    _openai_chat(client)

    assert len(finished_spans()) == 1


def test_llm_span_is_parented_to_enclosing_trace() -> None:
    client = _openai_client(200, OPENAI_OK)
    instrument_openai()

    with trace("agent-run") as run:
        _openai_chat(client)

    llm_span = next(s for s in finished_spans() if s.kind == "llm")
    assert llm_span.parent_id == run.span_id


def test_missing_openai_package_gives_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "openai.resources.chat.completions", None)

    with pytest.raises(ImportError, match="requires the 'openai' package"):
        instrument_openai()


def test_missing_anthropic_package_gives_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "anthropic.resources.messages", None)

    with pytest.raises(ImportError, match="requires the 'anthropic' package"):
        instrument_anthropic()
