import asyncio
import json
from collections.abc import Coroutine, Iterator

import httpx2
import pytest
from anthropic import Anthropic, AsyncAnthropic, Stream
from anthropic.resources.messages import AsyncMessages, Messages
from anthropic.types import Message, RawMessageStreamEvent
from openai import AsyncOpenAI, OpenAI
from openai import Stream as OpenAIStream
from openai.resources.chat.completions import AsyncCompletions, Completions
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from pydantic import TypeAdapter

from yolotrace.sdk import (
    current_span,
    finished_spans,
    instrument_anthropic,
    instrument_openai,
    trace,
)

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


def _openai_chunk(
    delta: dict[str, object],
    finish_reason: str | None = None,
    usage: dict[str, int] | None = None,
) -> dict[str, object]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "gpt-test",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]
        if delta or finish_reason
        else [],
        "usage": usage,
    }


OPENAI_CHUNKS = [
    _openai_chunk({"role": "assistant", "content": "h"}),
    _openai_chunk({"content": "i"}),
    _openai_chunk({}, finish_reason="stop"),
    _openai_chunk(
        {}, usage={"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}
    ),
]

ANTHROPIC_EVENTS: list[dict[str, object]] = [
    {
        "type": "message_start",
        "message": {
            **ANTHROPIC_OK,
            "content": [],
            "usage": {"input_tokens": 5, "output_tokens": 1},
        },
    },
    {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "hi"},
    },
    {"type": "content_block_stop", "index": 0},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 7},
    },
    {"type": "message_stop"},
]

_EVENT_ADAPTER: TypeAdapter[RawMessageStreamEvent] = TypeAdapter(RawMessageStreamEvent)


@pytest.fixture(autouse=True)
def restore_clients() -> Iterator[None]:
    originals = (
        Completions.create,
        AsyncCompletions.create,
        Messages.create,
        AsyncMessages.create,
    )
    yield
    Completions.create = originals[0]  # type: ignore[method-assign]
    AsyncCompletions.create = originals[1]  # type: ignore[method-assign]
    Messages.create = originals[2]  # type: ignore[method-assign]
    AsyncMessages.create = originals[3]  # type: ignore[method-assign]


def _openai_response(
    status: int = 200,
    json_body: dict[str, object] | None = None,
    sse: list[dict[str, object]] | None = None,
) -> httpx2.Response:
    if sse is not None:
        body = (
            "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in sse)
            + "data: [DONE]\n\n"
        )
        return httpx2.Response(
            status, content=body.encode(), headers={"content-type": "text/event-stream"}
        )
    return httpx2.Response(status, json=json_body)


def _openai_client(response: httpx2.Response) -> OpenAI:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return response

    http_client = httpx2.Client(transport=httpx2.MockTransport(handler))
    return OpenAI(api_key="test", http_client=http_client, max_retries=0)


def _async_openai_client(response: httpx2.Response) -> AsyncOpenAI:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return response

    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return AsyncOpenAI(api_key="test", http_client=http_client, max_retries=0)


def _anthropic_messages(
    stream_events: list[dict[str, object]] | None = None,
) -> Messages:
    """Messages resource whose transport returns ``stream_events`` as a stream."""

    def post(*args: object, **kwargs: object) -> Iterator[RawMessageStreamEvent]:
        assert stream_events is not None
        return iter([_EVENT_ADAPTER.validate_python(event) for event in stream_events])

    client = Anthropic(api_key="test", max_retries=0)
    client.post = post  # type: ignore[assignment]
    return Messages(client)


def _async_anthropic_messages(response: Message | None) -> AsyncMessages:
    async def post(*args: object, **kwargs: object) -> Message:
        if response is None:
            raise RuntimeError("bad input")
        return response

    client = AsyncAnthropic(api_key="test", max_retries=0)
    client.post = post  # type: ignore[assignment]
    return AsyncMessages(client)


def _openai_stream_call(client: OpenAI) -> OpenAIStream[ChatCompletionChunk]:
    return client.chat.completions.create(
        model="gpt-test",
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
        stream_options={"include_usage": True},
    )


def _anthropic_stream_call(messages: Messages) -> Stream[RawMessageStreamEvent]:
    return messages.create(
        model="claude-test",
        max_tokens=100,
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
    )


def _openai_async_call(
    client: AsyncOpenAI,
) -> Coroutine[object, object, ChatCompletion]:
    return client.chat.completions.create(
        model="gpt-test",
        messages=[{"role": "user", "content": "hello"}],
    )


def _anthropic_async_call(
    messages: AsyncMessages,
) -> Coroutine[object, object, Message]:
    return messages.create(
        model="claude-test",
        max_tokens=100,
        messages=[{"role": "user", "content": "hello"}],
    )


def test_openai_stream_span_ends_only_after_stream_is_consumed() -> None:
    client = _openai_client(_openai_response(sse=OPENAI_CHUNKS))
    instrument_openai()

    stream = _openai_stream_call(client)
    assert finished_spans() == []

    text = "".join(
        chunk.choices[0].delta.content or "" for chunk in stream if chunk.choices
    )

    assert text == "hi"
    (span,) = finished_spans()
    assert span.name == "openai.chat.completions.create"
    assert span.kind == "llm"
    assert span.status == "ok"
    assert span.end_time is not None
    assert span.attributes == {
        "provider": "openai",
        "model": "gpt-test",
        "input_tokens": 5,
        "output_tokens": 7,
    }


def test_openai_stream_without_usage_still_records_span() -> None:
    client = _openai_client(_openai_response(sse=OPENAI_CHUNKS[:3]))
    instrument_openai()

    list(_openai_stream_call(client))

    (span,) = finished_spans()
    assert span.status == "ok"
    assert "input_tokens" not in span.attributes


def test_openai_stream_closed_early_ends_span() -> None:
    client = _openai_client(_openai_response(sse=OPENAI_CHUNKS))
    instrument_openai()

    stream = _openai_stream_call(client)
    next(iter(stream))
    stream.close()

    (span,) = finished_spans()
    assert span.status == "ok"
    assert span.end_time is not None


def test_openai_stream_with_context_manager_ends_span() -> None:
    client = _openai_client(_openai_response(sse=OPENAI_CHUNKS))
    instrument_openai()

    with _openai_stream_call(client) as stream:
        assert finished_spans() == []
        list(stream)

    (span,) = finished_spans()
    assert span.end_time is not None


def test_failed_openai_stream_request_is_recorded_and_reraised() -> None:
    client = _openai_client(
        _openai_response(
            status=400, json_body={"error": {"message": "bad input", "type": "x"}}
        )
    )
    instrument_openai()

    with pytest.raises(Exception, match="bad input"):
        _openai_stream_call(client)

    (span,) = finished_spans()
    assert span.status == "error"
    assert span.error is not None and "bad input" in span.error


def test_streaming_openai_span_is_parented_to_enclosing_trace() -> None:
    client = _openai_client(_openai_response(sse=OPENAI_CHUNKS))
    instrument_openai()

    with trace("agent-run") as run:
        stream = _openai_stream_call(client)
    list(stream)

    llm_span = next(s for s in finished_spans() if s.kind == "llm")
    assert llm_span.parent_id == run.span_id
    assert current_span() is None


def test_anthropic_stream_records_usage_from_start_and_delta_events() -> None:
    messages = _anthropic_messages(ANTHROPIC_EVENTS)
    instrument_anthropic()

    stream = _anthropic_stream_call(messages)
    assert finished_spans() == []
    events = list(stream)

    assert len(events) == len(ANTHROPIC_EVENTS)
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


def test_async_openai_call_is_recorded_as_llm_span() -> None:
    client = _async_openai_client(_openai_response(json_body=OPENAI_OK))
    instrument_openai()

    response = asyncio.run(_openai_async_call(client))

    assert response.choices[0].message.content == "hi"
    (span,) = finished_spans()
    assert span.name == "openai.chat.completions.create"
    assert span.kind == "llm"
    assert span.status == "ok"
    assert span.attributes == {
        "provider": "openai",
        "model": "gpt-test",
        "input_tokens": 5,
        "output_tokens": 7,
    }


def test_failed_async_openai_call_is_recorded_and_reraised() -> None:
    client = _async_openai_client(
        _openai_response(
            status=400, json_body={"error": {"message": "bad input", "type": "x"}}
        )
    )
    instrument_openai()

    with pytest.raises(Exception, match="bad input"):
        asyncio.run(_openai_async_call(client))

    (span,) = finished_spans()
    assert span.status == "error"
    assert span.error is not None and "bad input" in span.error


def test_async_anthropic_call_is_recorded_as_llm_span() -> None:
    messages = _async_anthropic_messages(Message.model_validate(ANTHROPIC_OK))
    instrument_anthropic()

    response = asyncio.run(_anthropic_async_call(messages))

    assert response.content[0].text == "hi"  # type: ignore[union-attr]
    (span,) = finished_spans()
    assert span.name == "anthropic.messages.create"
    assert span.attributes == {
        "provider": "anthropic",
        "model": "claude-test",
        "input_tokens": 5,
        "output_tokens": 7,
    }


def test_failed_async_anthropic_call_is_recorded_and_reraised() -> None:
    messages = _async_anthropic_messages(None)
    instrument_anthropic()

    with pytest.raises(RuntimeError, match="bad input"):
        asyncio.run(_anthropic_async_call(messages))

    (span,) = finished_spans()
    assert span.status == "error"
    assert span.error == "RuntimeError: bad input"


def test_async_openai_stream_span_ends_after_async_iteration() -> None:
    client = _async_openai_client(_openai_response(sse=OPENAI_CHUNKS))
    instrument_openai()

    async def consume() -> str:
        stream = await client.chat.completions.create(
            model="gpt-test",
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
            stream_options={"include_usage": True},
        )
        assert finished_spans() == []
        parts = [
            chunk.choices[0].delta.content or ""
            async for chunk in stream
            if chunk.choices
        ]
        return "".join(parts)

    assert asyncio.run(consume()) == "hi"
    (span,) = finished_spans()
    assert span.status == "ok"
    assert span.attributes["input_tokens"] == 5
    assert span.attributes["output_tokens"] == 7


def test_async_openai_stream_closed_early_ends_span() -> None:
    client = _async_openai_client(_openai_response(sse=OPENAI_CHUNKS))
    instrument_openai()

    async def consume() -> None:
        stream = await client.chat.completions.create(
            model="gpt-test",
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
        )
        await stream.__anext__()
        await stream.close()

    asyncio.run(consume())

    (span,) = finished_spans()
    assert span.end_time is not None


def test_concurrent_async_openai_calls_are_each_parented_to_the_run() -> None:
    client = _async_openai_client(_openai_response(json_body=OPENAI_OK))
    instrument_openai()

    async def run() -> None:
        with trace("agent-run") as run_span:
            await asyncio.gather(*(_openai_async_call(client) for _ in range(3)))
        assert run_span.end_time is not None

    asyncio.run(run())

    spans = finished_spans()
    run_span = next(s for s in spans if s.name == "agent-run")
    llm_spans = [s for s in spans if s.kind == "llm"]
    assert len(llm_spans) == 3
    assert all(s.parent_id == run_span.span_id for s in llm_spans)
    assert len({s.span_id for s in llm_spans}) == 3
