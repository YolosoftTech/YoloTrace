"""HTTP server that accepts JSON trace batches on ``POST /v1/spans`` and OTLP/HTTP JSON on ``POST /v1/traces``."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from yolotrace.ingestion.otlp import parse_otlp_request
from yolotrace.ingestion.parsing import IngestionError, parse_batch
from yolotrace.ingestion.store import TraceStore
from yolotrace.sdk.export import JsonValue

INGEST_PATH = "/v1/spans"
"""The route that accepts a batch of spans in the YoloTrace JSON format."""

OTLP_TRACES_PATH = "/v1/traces"
"""The OpenTelemetry route that accepts OTLP/HTTP JSON trace requests."""

DEFAULT_PORT = 4318
"""Port the ``serve`` command listens on by default, the same as the OpenTelemetry HTTP port."""

MAX_BODY_BYTES = 16 * 1024 * 1024
"""Largest request body the server reads. A bigger request is refused with HTTP 413."""


class IngestionServer(ThreadingHTTPServer):
    """An HTTP server whose handlers add every span they accept to ``store``."""

    def __init__(self, address: tuple[str, int], store: TraceStore) -> None:
        super().__init__(address, _IngestionHandler)
        self.store = store


class _IngestionHandler(BaseHTTPRequestHandler):
    """Answer ingestion requests with JSON. Every response is either the accepted count or an ``error`` message."""

    server: IngestionServer

    def do_POST(self) -> None:
        """Route a request to the handler for its path. Each handler stores the spans it accepts."""
        path = urlsplit(self.path).path
        if path not in (INGEST_PATH, OTLP_TRACES_PATH):
            self._discard_body()
            self._reply(
                404,
                {
                    "error": f"no endpoint at {self.path}; send spans to {INGEST_PATH} "
                    f"or OpenTelemetry traces to {OTLP_TRACES_PATH}"
                },
            )
            return
        body = self._read_body()
        if body is None:
            return
        if path == OTLP_TRACES_PATH:
            self._accept_otlp(body)
        else:
            self._accept_spans(body)

    def _read_body(self) -> bytes | None:
        """Return the request body, or ``None`` after replying with an error when it cannot be read."""
        length = self.headers.get("Content-Length")
        if length is None:
            self._reply(411, {"error": "a Content-Length header is required"})
            return None
        if not length.isdigit():
            self._reply(
                400, {"error": "Content-Length must be a whole number of bytes"}
            )
            return None
        if int(length) > MAX_BODY_BYTES:
            self._reply(413, {"error": f"body is larger than {MAX_BODY_BYTES} bytes"})
            return None
        return self.rfile.read(int(length))

    def _accept_spans(self, body: bytes) -> None:
        """Store the batch in ``body`` and answer 202 with how many spans were accepted."""
        try:
            spans = parse_batch(body)
        except IngestionError as error:
            self._reply(400, {"error": str(error)})
            return
        self.server.store.add(spans)
        self._reply(202, {"accepted": len(spans)})

    def _accept_otlp(self, body: bytes) -> None:
        """Store the OTLP/HTTP JSON request in ``body`` and answer 200 with the empty success response of OTLP."""
        content_type = (
            self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        )
        if content_type != "application/json":
            self._reply(
                415,
                {
                    "error": "OTLP requests must use Content-Type application/json; "
                    "protobuf is not supported, so set the exporter protocol to http/json"
                },
            )
            return
        if self.headers.get("Content-Encoding", "identity").lower() != "identity":
            self._reply(
                415,
                {
                    "error": "compressed OTLP requests are not supported; turn off compression in the exporter"
                },
            )
            return
        try:
            spans = parse_otlp_request(body)
        except IngestionError as error:
            self._reply(400, {"error": str(error)})
            return
        self.server.store.add(spans)
        self._reply(200, {})

    def _discard_body(self) -> None:
        """Read and drop a small request body, so the client can read the reply without the connection resetting."""
        length = self.headers.get("Content-Length")
        if length is not None and length.isdigit() and int(length) <= MAX_BODY_BYTES:
            self.rfile.read(int(length))

    def _reply(self, status: int, payload: dict[str, JsonValue]) -> None:
        """Send ``payload`` as a JSON response with ``status``."""
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_ingestion_server(host: str, port: int) -> None:
    """Accept spans at ``http://host:port/v1/spans`` and traces at ``/v1/traces`` until the process stops."""
    with IngestionServer((host, port), TraceStore()) as server:
        server.serve_forever()
