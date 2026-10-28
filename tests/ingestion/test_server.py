import http.client
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from yolotrace.ingestion.server import (
    INGEST_PATH,
    MAX_BODY_BYTES,
    OTLP_TRACES_PATH,
    IngestionServer,
)
from yolotrace.ingestion.store import TraceStore
from yolotrace.sdk.export import SCHEMA_VERSION, JsonValue


@dataclass(frozen=True, slots=True)
class _Response:
    """The status, body, and parsed JSON of one HTTP response."""

    status: int
    body: bytes

    @property
    def json(self) -> dict[str, str | int]:
        """Return the body parsed as a JSON object."""
        value = json.loads(self.body)
        assert isinstance(value, dict)
        return value


@pytest.fixture
def running_server() -> Iterator[tuple[IngestionServer, TraceStore]]:
    """Serve the ingestion API on a free local port for the duration of one test."""
    store = TraceStore()
    server = IngestionServer(("127.0.0.1", 0), store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, store
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _span(name: str = "answer") -> dict[str, JsonValue]:
    """Return one span in the JSON format."""
    return {
        "name": name,
        "trace_id": "0af7651916cd43dd8448eb211c80319c",
        "span_id": "b7ad6b7169203331",
        "parent_id": None,
        "kind": "step",
        "start_time": 1.0,
        "end_time": 2.0,
        "duration": 1.0,
        "status": "ok",
        "error": None,
        "attributes": {},
        "events": [],
        "sampled": True,
    }


def _post(server: IngestionServer, path: str, body: bytes) -> _Response:
    """Send one POST request with ``body`` to ``server`` and return its response."""
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        return _Response(status=response.status, body=response.read())
    finally:
        connection.close()


def _batch(*spans: dict[str, JsonValue]) -> bytes:
    """Return a batch body holding ``spans``."""
    return json.dumps({"schema_version": SCHEMA_VERSION, "spans": list(spans)}).encode()


def test_valid_batch_is_stored_and_acknowledged(
    running_server: tuple[IngestionServer, TraceStore],
) -> None:
    server, store = running_server

    response = _post(server, INGEST_PATH, _batch(_span("first"), _span("second")))

    assert response.status == 202
    assert response.json == {"accepted": 2}
    assert [span.name for span in store.spans()] == ["first", "second"]


def test_batches_accumulate_in_the_store(
    running_server: tuple[IngestionServer, TraceStore],
) -> None:
    server, store = running_server

    _post(server, INGEST_PATH, _batch(_span("first")))
    _post(server, INGEST_PATH, _batch(_span("second")))

    assert [span.name for span in store.spans()] == ["first", "second"]


def test_invalid_batch_is_refused_with_the_reason_and_stores_nothing(
    running_server: tuple[IngestionServer, TraceStore],
) -> None:
    server, store = running_server
    bad = _span()
    bad["kind"] = "pipeline"

    response = _post(server, INGEST_PATH, _batch(_span(), bad))

    assert response.status == 400
    assert "body.spans[1].kind must be one of run, step, llm, tool" in str(
        response.json["error"]
    )
    assert store.spans() == []


def test_body_that_is_not_json_is_refused(
    running_server: tuple[IngestionServer, TraceStore],
) -> None:
    server, store = running_server

    response = _post(server, INGEST_PATH, b"{not json")

    assert response.status == 400
    assert str(response.json["error"]).startswith("body is not valid JSON")
    assert store.spans() == []


def test_unknown_path_is_not_found(
    running_server: tuple[IngestionServer, TraceStore],
) -> None:
    server, store = running_server

    response = _post(server, "/v2/spans", _batch(_span()))

    assert response.status == 404
    assert "send spans to /v1/spans" in str(response.json["error"])
    assert "traces to /v1/traces" in str(response.json["error"])
    assert store.spans() == []


def test_query_string_on_the_ingest_path_is_ignored(
    running_server: tuple[IngestionServer, TraceStore],
) -> None:
    server, store = running_server

    response = _post(server, f"{INGEST_PATH}?source=test", _batch(_span()))

    assert response.status == 202
    assert len(store.spans()) == 1


def test_request_without_content_length_is_refused(
    running_server: tuple[IngestionServer, TraceStore],
) -> None:
    server, store = running_server
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.putrequest("POST", INGEST_PATH, skip_accept_encoding=True)
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
    finally:
        connection.close()

    assert response.status == 411
    assert b"Content-Length header is required" in body
    assert store.spans() == []


def test_body_over_the_size_limit_is_refused_before_it_is_read(
    running_server: tuple[IngestionServer, TraceStore],
) -> None:
    server, store = running_server
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.putrequest("POST", INGEST_PATH, skip_accept_encoding=True)
        connection.putheader("Content-Length", str(MAX_BODY_BYTES + 1))
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
    finally:
        connection.close()

    assert response.status == 413
    assert b"larger than" in body
    assert store.spans() == []


def _otlp_request(name: str = "answer") -> bytes:
    """Return an OTLP/HTTP JSON request holding one valid span named ``name``."""
    span: dict[str, JsonValue] = {
        "traceId": "0af7651916cd43dd8448eb211c80319c",
        "spanId": "b7ad6b7169203331",
        "name": name,
        "startTimeUnixNano": "1000500000000",
        "endTimeUnixNano": "1001000000000",
    }
    return json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [span]}]}]}).encode()


def _post_with_headers(
    server: IngestionServer, path: str, body: bytes, headers: dict[str, str]
) -> _Response:
    """Send one POST request with ``body`` and ``headers`` to ``server`` and return its response."""
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        return _Response(status=response.status, body=response.read())
    finally:
        connection.close()


def test_otlp_json_request_is_stored_and_acknowledged_with_an_empty_object(
    running_server: tuple[IngestionServer, TraceStore],
) -> None:
    server, store = running_server

    response = _post(server, OTLP_TRACES_PATH, _otlp_request("answer"))

    assert response.status == 200
    assert response.json == {}
    assert [span.name for span in store.spans()] == ["answer"]


def test_invalid_otlp_request_is_refused_and_stores_nothing(
    running_server: tuple[IngestionServer, TraceStore],
) -> None:
    server, store = running_server

    response = _post(
        server,
        OTLP_TRACES_PATH,
        b'{"resourceSpans": [{"scopeSpans": [{"spans": [{}]}]}]}',
    )

    assert response.status == 400
    assert "is required" in str(response.json["error"])
    assert store.spans() == []


def test_otlp_protobuf_request_is_refused_with_the_protocol_to_use(
    running_server: tuple[IngestionServer, TraceStore],
) -> None:
    server, store = running_server

    response = _post_with_headers(
        server,
        OTLP_TRACES_PATH,
        b"protobuf-encoded-request",
        {"Content-Type": "application/x-protobuf"},
    )

    assert response.status == 415
    assert "http/json" in str(response.json["error"])
    assert store.spans() == []


def test_compressed_otlp_request_is_refused(
    running_server: tuple[IngestionServer, TraceStore],
) -> None:
    server, store = running_server

    response = _post_with_headers(
        server,
        OTLP_TRACES_PATH,
        _otlp_request(),
        {"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )

    assert response.status == 415
    assert "compressed" in str(response.json["error"])
    assert store.spans() == []


def test_otlp_request_without_content_length_is_refused(
    running_server: tuple[IngestionServer, TraceStore],
) -> None:
    server, store = running_server
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.putrequest("POST", OTLP_TRACES_PATH, skip_accept_encoding=True)
        connection.endheaders()
        response = connection.getresponse()
        response.read()
    finally:
        connection.close()

    assert response.status == 411
    assert store.spans() == []
