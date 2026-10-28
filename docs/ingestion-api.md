# YoloTrace ingestion API

The ingestion API accepts finished spans from the SDK over HTTP. It uses the trace, span, and event model in [data-model.md](data-model.md), written as JSON with the same field names.

## Running the server

```bash
python -m yolotrace serve --host 127.0.0.1 --port 4318
```

The server listens on `http://127.0.0.1:4318/v1/spans` by default. It has no authentication yet, so it listens on localhost unless you pass `--host` explicitly. Only expose it on a network you trust.

Accepted spans are kept in the server's memory and are lost when it stops. Persistent storage is not built yet.

## Sending spans from the SDK

```python
from yolotrace import export_spans, finished_spans

sent = export_spans("http://127.0.0.1:4318/v1/spans")  # every finished span in this process
```

`export_spans(endpoint, spans=None, *, timeout=10.0)`:

- `endpoint` is the full URL of the route. It must start with `http://` or `https://`, otherwise `ValueError` is raised.
- `spans` defaults to `finished_spans()`. Pass a list to send only some spans.
- The JavaScript/TypeScript SDK has `exportSpans(endpoint, spans?, { timeoutMs? })`, and the Go SDK has `ExportSpans(ctx, endpoint, spans)`. They send the same format and follow the same rules.
- It returns the number of spans sent.
- Spans are sent in batches of 500. A batch the server does not accept raises `ExportError`. Batches sent before it stay stored, so exporting the same spans again stores them again. Deduplication is not built yet.

## Endpoint

### `POST /v1/spans`

Request body: a JSON object.

| Field | Type | Rules |
| --- | --- | --- |
| `schema_version` | integer | Required. Must be `1`. |
| `spans` | array | Required. Each item is a span object, below. An empty array is valid. |

Request headers: `Content-Length` is required. The body must be at most 16 MiB. `Content-Type: application/json` is recommended.

### Span object

| Field | Type | Rules |
| --- | --- | --- |
| `name` | string | Required. |
| `trace_id` | string | Required. 32 lowercase hexadecimal characters. |
| `span_id` | string | Required. 16 lowercase hexadecimal characters. |
| `parent_id` | string or null | Required. 16 lowercase hexadecimal characters, or `null` for a root span. |
| `kind` | string | Required. One of `run`, `step`, `llm`, `tool`. |
| `start_time` | number | Required. Unix time in seconds. |
| `end_time` | number or null | Required. Unix time in seconds, or `null` while the span is running. |
| `duration` | number or null | Required. Seconds, or `null` while the span is running. |
| `status` | string | Required. `ok` or `error`. |
| `error` | string or null | Required. `"Type: message"` for a failed span, otherwise `null`. |
| `attributes` | object | Required. Values are strings, numbers, booleans, or `null`. |
| `events` | array | Required. Each item is an event object, below. |
| `sampled` | boolean | Required. |

### Event object

| Field | Type | Rules |
| --- | --- | --- |
| `name` | string | Required. |
| `time` | number | Required. Unix time in seconds. |
| `attributes` | object | Required. Same rules as span attributes. |

### Example

```json
{
  "schema_version": 1,
  "spans": [
    {
      "name": "answer",
      "trace_id": "0af7651916cd43dd8448eb211c80319c",
      "span_id": "b7ad6b7169203331",
      "parent_id": null,
      "kind": "run",
      "start_time": 1000.5,
      "end_time": 1001.0,
      "duration": 0.5,
      "status": "ok",
      "error": null,
      "attributes": {"model": "gpt-test", "tokens": 12},
      "events": [{"name": "retry", "time": 1000.6, "attributes": {"attempt": 2}}],
      "sampled": true
    }
  ]
}
```

### Responses

| Status | When | Body |
| --- | --- | --- |
| `202 Accepted` | Every span is valid and stored. | `{"accepted": <count>}` |
| `400 Bad Request` | The body is not JSON, or a field is missing or has the wrong type. Nothing from the batch is stored. | `{"error": "<the first invalid field, such as body.spans[1].kind must be one of run, step, llm, tool>"}` |
| `404 Not Found` | The path is not `/v1/spans`. | `{"error": "..."}` |
| `411 Length Required` | No `Content-Length` header. | `{"error": "..."}` |
| `413 Content Too Large` | The body is larger than 16 MiB. | `{"error": "..."}` |

A batch is stored only when every span in it is valid, so a rejected batch never leaves part of itself behind.

## Not yet supported

- Authentication, signed credentials, and network restrictions.
- Persistent storage, retention, and deduplication of retried spans.
- Validation limits on event counts and attribute counts; the SDK's own limits (`configure`) apply at the source.
- Out-of-order and partially completed runs are stored as received, without any reconciliation.
- Health checks, batch compression, and OpenTelemetry formats.
