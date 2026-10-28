"""HTTP server that accepts JSON trace batches on ``POST /v1/spans``."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from yolotrace.ingestion.parsing import IngestionError, parse_batch
from yolotrace.ingestion.store import TraceStore
from yolotrace.sdk.export import JsonValue

INGEST_PATH = "/v1/spans"
"""The route that accepts a batch of spans."""

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
        """Accept a batch on ``INGEST_PATH``, store its spans, and answer 202 with how many were accepted."""
        if urlsplit(self.path).path != INGEST_PATH:
            self._discard_body()
            self._reply(
                404,
                {"error": f"no endpoint at {self.path}; send spans to {INGEST_PATH}"},
            )
            return
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            self._reply(411, {"error": "a Content-Length header is required"})
            return
        if int(length) > MAX_BODY_BYTES:
            self._reply(413, {"error": f"body is larger than {MAX_BODY_BYTES} bytes"})
            return
        try:
            spans = parse_batch(self.rfile.read(int(length)))
        except IngestionError as error:
            self._reply(400, {"error": str(error)})
            return
        self.server.store.add(spans)
        self._reply(202, {"accepted": len(spans)})

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
    """Accept spans at ``http://host:port/v1/spans`` until the process stops."""
    with IngestionServer((host, port), TraceStore()) as server:
        server.serve_forever()
