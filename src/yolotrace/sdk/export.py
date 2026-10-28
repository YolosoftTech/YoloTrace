"""Send finished spans to an ingestion endpoint over HTTP, in the JSON format of docs/ingestion-api.md."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterable, Sequence
from urllib.parse import urlsplit

from yolotrace.sdk.tracing import Span, finished_spans

type JsonValue = (
    str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
)
"""A value of the JSON format: what ``json.loads`` can produce."""

SCHEMA_VERSION = 1
"""Version of the JSON trace format. The ingestion API rejects any other value."""

BATCH_SIZE = 500
"""Most spans sent in one request. A larger export is split into several requests."""


class ExportError(Exception):
    """Raised when the ingestion endpoint cannot be reached or does not accept spans."""


def span_to_dict(span: Span) -> dict[str, JsonValue]:
    """Return ``span`` in the JSON form the ingestion API accepts, using the data model's field names."""
    return {
        "name": span.name,
        "trace_id": span.trace_id,
        "span_id": span.span_id,
        "parent_id": span.parent_id,
        "kind": span.kind,
        "start_time": span.start_time,
        "end_time": span.end_time,
        "duration": span.duration,
        "status": span.status,
        "error": span.error,
        "attributes": dict(span.attributes),
        "events": [
            {
                "name": event.name,
                "time": event.time,
                "attributes": dict(event.attributes),
            }
            for event in span.events
        ],
        "sampled": span.sampled,
    }


def export_spans(
    endpoint: str,
    spans: Iterable[Span] | None = None,
    *,
    timeout: float = 10.0,
) -> int:
    """Send ``spans`` to the ingestion ``endpoint`` and return how many were sent.

    ``spans`` defaults to every finished span in this process. ``endpoint`` is the full URL
    of the ingestion route, such as ``http://127.0.0.1:4318/v1/spans``. Spans go in batches
    of ``BATCH_SIZE``. Raises ``ValueError`` for a URL that is not http or https, and
    ``ExportError`` when a batch cannot be delivered. Batches already accepted stay accepted,
    so exporting the same spans again stores them again.
    """
    if urlsplit(endpoint).scheme not in ("http", "https"):
        raise ValueError(f"endpoint must be an http or https URL: {endpoint!r}")
    batch_source = list(finished_spans() if spans is None else spans)
    for start in range(0, len(batch_source), BATCH_SIZE):
        _post(endpoint, batch_source[start : start + BATCH_SIZE], timeout)
    return len(batch_source)


def _post(endpoint: str, batch: Sequence[Span], timeout: float) -> None:
    """POST one batch of spans to ``endpoint`` and raise ``ExportError`` unless it is accepted."""
    body = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "spans": [span_to_dict(span) for span in batch],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        # The scheme is checked by export_spans, so only http and https URLs reach here.
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            response.read()
    except urllib.error.HTTPError as error:
        raise ExportError(
            f"ingestion endpoint refused {len(batch)} spans with HTTP {error.code}"
        ) from error
    except OSError as error:
        raise ExportError(f"could not reach {endpoint}: {error}") from error
