import json
import threading
from collections.abc import Iterator

import pytest

from yolotrace.ingestion.parsing import parse_batch
from yolotrace.ingestion.server import INGEST_PATH, IngestionServer
from yolotrace.ingestion.store import TraceStore
from yolotrace.sdk import export as export_module
from yolotrace.sdk.export import (
    BATCH_SIZE,
    SCHEMA_VERSION,
    ExportError,
    export_spans,
    span_to_dict,
)
from yolotrace.sdk.tracing import Span, SpanEvent, finished_spans, trace


@pytest.fixture
def ingestion() -> Iterator[tuple[str, TraceStore]]:
    """Run an ingestion server on a free local port and yield its URL and store."""
    store = TraceStore()
    server = IngestionServer(("127.0.0.1", 0), store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}{INGEST_PATH}", store
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _span(name: str, span_id: str = "b7ad6b7169203331") -> Span:
    """Return a finished span named ``name`` with ``span_id``."""
    return Span(
        name=name,
        trace_id="0af7651916cd43dd8448eb211c80319c",
        span_id=span_id,
        parent_id=None,
        start_time=1.0,
        end_time=2.0,
        _duration=1.0,
    )


def test_span_to_dict_uses_the_data_model_field_names() -> None:
    span = Span(
        name="tool",
        trace_id="0af7651916cd43dd8448eb211c80319c",
        span_id="b7ad6b7169203331",
        parent_id="1111111111111111",
        start_time=1.0,
        end_time=3.0,
        status="error",
        error="ValueError: bad",
        kind="tool",
        attributes={"bytes": 1024},
        events=[SpanEvent(name="retry", time=2.0, attributes={"attempt": 2})],
        _duration=2.0,
    )

    assert span_to_dict(span) == {
        "name": "tool",
        "trace_id": "0af7651916cd43dd8448eb211c80319c",
        "span_id": "b7ad6b7169203331",
        "parent_id": "1111111111111111",
        "kind": "tool",
        "start_time": 1.0,
        "end_time": 3.0,
        "duration": 2.0,
        "status": "error",
        "error": "ValueError: bad",
        "attributes": {"bytes": 1024},
        "events": [{"name": "retry", "time": 2.0, "attributes": {"attempt": 2}}],
        "sampled": True,
    }


def test_export_without_spans_sends_every_finished_span(
    ingestion: tuple[str, TraceStore],
) -> None:
    url, store = ingestion
    with trace("first"):
        pass
    with trace("second"):
        pass

    sent = export_spans(url)

    assert sent == 2
    assert [span.name for span in store.spans()] == ["first", "second"]


def test_exported_spans_keep_their_identity_and_events(
    ingestion: tuple[str, TraceStore],
) -> None:
    url, store = ingestion
    with trace("llm", kind="llm", attributes={"model": "m"}) as span:
        span.add_event("chunk", {"index": 1})

    export_spans(url)

    (received,) = store.spans()
    assert span_to_dict(received) == span_to_dict(span)


def test_export_of_explicit_spans_sends_only_those(
    ingestion: tuple[str, TraceStore],
) -> None:
    url, store = ingestion
    with trace("kept"):
        pass
    with trace("skipped"):
        pass
    kept = finished_spans()[0]

    sent = export_spans(url, [kept])

    assert sent == 1
    assert [span.name for span in store.spans()] == ["kept"]


def test_export_with_no_spans_sends_nothing(ingestion: tuple[str, TraceStore]) -> None:
    url, store = ingestion

    assert export_spans(url) == 0
    assert store.spans() == []


def test_large_export_is_split_into_batches(
    ingestion: tuple[str, TraceStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, store = ingestion
    spans = [
        _span(f"span-{index}", span_id=f"{index:016x}")
        for index in range(BATCH_SIZE + 3)
    ]
    batch_sizes: list[int] = []
    real_post = export_module._post

    def recording_post(endpoint: str, batch: list[Span], timeout: float) -> None:
        batch_sizes.append(len(batch))
        real_post(endpoint, batch, timeout)

    monkeypatch.setattr(export_module, "_post", recording_post)

    assert export_spans(url, spans) == BATCH_SIZE + 3
    assert batch_sizes == [BATCH_SIZE, 3]
    assert len(store.spans()) == BATCH_SIZE + 3


def test_export_to_a_non_http_url_is_rejected() -> None:
    with pytest.raises(ValueError, match="http or https"):
        export_spans("file:///tmp/spans.json")


def test_export_to_unreachable_endpoint_raises_export_error() -> None:
    with pytest.raises(ExportError, match="could not reach"):
        export_spans("http://127.0.0.1:1/v1/spans", [_span("lost")], timeout=2)


def test_export_refused_by_the_endpoint_raises_export_error(
    ingestion: tuple[str, TraceStore],
) -> None:
    url, store = ingestion
    wrong_url = url.replace(INGEST_PATH, "/v2/spans")

    with pytest.raises(ExportError, match="refused 1 spans with HTTP 404"):
        export_spans(wrong_url, [_span("rejected")])
    assert store.spans() == []


def test_exported_batch_is_valid_for_the_ingestion_parser() -> None:
    body = json.dumps(
        {"schema_version": SCHEMA_VERSION, "spans": [span_to_dict(_span("check"))]}
    ).encode()

    (parsed,) = parse_batch(body)

    assert parsed.name == "check"
