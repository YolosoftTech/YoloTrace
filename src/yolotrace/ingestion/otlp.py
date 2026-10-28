"""Turn OpenTelemetry trace requests in OTLP/HTTP JSON form into spans. See docs/ingestion-api.md."""

from __future__ import annotations

import json
import re

from yolotrace.ingestion.parsing import (
    IngestionError,
    _as_list,
    _as_object,
    _expect_hex,
    _expect_number,
    _expect_str,
    _field,
)
from yolotrace.sdk.export import JsonValue
from yolotrace.sdk.tracing import AttributeValue, Span, SpanEvent, SpanKind

_LLM_OPERATIONS = ("chat", "text_completion", "generate_content", "embeddings")
_NANOS_PER_SECOND = 1_000_000_000


def parse_otlp_request(body: bytes) -> list[Span]:
    """Parse an OTLP/HTTP JSON ``ExportTraceServiceRequest`` into spans, in the order they were sent.

    Resource attributes are copied onto each span, and the span's own attributes win on a clash.
    Raises ``IngestionError`` naming the first invalid part of the request, so nothing is returned
    for a request that is only partly valid.
    """
    try:
        document: JsonValue = json.loads(body)
    except ValueError as error:
        raise IngestionError(f"body is not valid JSON: {error}") from error
    request = _as_object(document, "body")
    spans: list[Span] = []
    for resource_index, resource_item in enumerate(
        _optional_list(request, "resourceSpans", "body")
    ):
        resource_where = f"body.resourceSpans[{resource_index}]"
        resource_span = _as_object(resource_item, resource_where)
        resource_attributes = _key_values(
            _optional_object(resource_span, "resource", resource_where),
            f"{resource_where}.resource",
        )
        for scope_index, scope_item in enumerate(
            _optional_list(resource_span, "scopeSpans", resource_where)
        ):
            scope_where = f"{resource_where}.scopeSpans[{scope_index}]"
            scope_span = _as_object(scope_item, scope_where)
            for span_index, span_item in enumerate(
                _optional_list(scope_span, "spans", scope_where)
            ):
                where = f"{scope_where}.spans[{span_index}]"
                spans.append(
                    _span_from_otlp(
                        _as_object(span_item, where), where, resource_attributes
                    )
                )
    return spans


def _span_from_otlp(
    data: dict[str, JsonValue],
    where: str,
    resource_attributes: dict[str, AttributeValue],
) -> Span:
    """Build a span from one OTLP span object. ``where`` names the object in error messages."""
    start_nanos = _unix_nanos(data, "startTimeUnixNano", where)
    end_nanos = _unix_nanos(data, "endTimeUnixNano", where)
    if end_nanos < start_nanos:
        raise IngestionError(
            f"{where}.endTimeUnixNano must not be before {where}.startTimeUnixNano"
        )
    attributes = {**resource_attributes, **_key_values(data, where)}
    events = _events(data, where)
    parent_id = _parent_id(data, where)
    status = _optional_object(data, "status", where)
    is_error = _is_error(status, f"{where}.status")
    return Span(
        name=_expect_str(data, "name", where),
        trace_id=_expect_hex(data, "traceId", where, length=32),
        span_id=_expect_hex(data, "spanId", where, length=16),
        parent_id=parent_id,
        start_time=start_nanos / _NANOS_PER_SECOND,
        end_time=end_nanos / _NANOS_PER_SECOND,
        status="error" if is_error else "ok",
        error=_error_text(events, status, is_error),
        kind=_span_kind(attributes, parent_id),
        attributes=attributes,
        events=events,
        sampled=True,
        _duration=(end_nanos - start_nanos) / _NANOS_PER_SECOND,
    )


def _span_kind(
    attributes: dict[str, AttributeValue], parent_id: str | None
) -> SpanKind:
    """Return the YoloTrace kind of a span, from its GenAI semantic-convention attributes.

    ``execute_tool`` is a tool, a chat or completion call with a model is an LLM call, and
    ``invoke_agent`` is a run. Any other span is a run when it has no parent, otherwise a step.
    """
    operation = attributes.get("gen_ai.operation.name")
    if operation == "execute_tool":
        return "tool"
    if operation in ("invoke_agent", "create_agent"):
        return "run"
    if operation in _LLM_OPERATIONS or "gen_ai.request.model" in attributes:
        return "llm"
    return "run" if parent_id is None else "step"


def _events(data: dict[str, JsonValue], where: str) -> list[SpanEvent]:
    """Return the events of a span, each with a name, a time in seconds and attributes."""
    events: list[SpanEvent] = []
    for index, item in enumerate(_optional_list(data, "events", where)):
        where_event = f"{where}.events[{index}]"
        event = _as_object(item, where_event)
        events.append(
            SpanEvent(
                name=_expect_str(event, "name", where_event),
                time=_unix_nanos(event, "timeUnixNano", where_event)
                / _NANOS_PER_SECOND,
                attributes=_key_values(event, where_event),
            )
        )
    return events


def _is_error(status: dict[str, JsonValue], where: str) -> bool:
    """Return whether the OTLP status ``status`` marks the span as failed.

    The code is the number from the OTLP enum or its name, as both are valid in JSON.
    """
    code = status.get("code", 0)
    if isinstance(code, bool):
        raise IngestionError(f"{where}.code must be one of 0, 1, 2 or their names")
    if code in (0, 1, "STATUS_CODE_UNSET", "STATUS_CODE_OK"):
        return False
    if code in (2, "STATUS_CODE_ERROR"):
        return True
    raise IngestionError(f"{where}.code must be one of 0, 1, 2 or their names")


def _error_text(
    events: list[SpanEvent], status: dict[str, JsonValue], is_error: bool
) -> str | None:
    """Return the ``"Type: message"`` text of a failed span, or ``None`` for a span that did not fail.

    The first ``exception`` event gives the text. Without one, the status message is used, and
    a failed span with neither reads ``Error``.
    """
    if not is_error:
        return None
    for event in events:
        if event.name == "exception" and "exception.type" in event.attributes:
            message = event.attributes.get("exception.message", "")
            return f"{event.attributes['exception.type']}: {message}"
    status_message = status.get("message")
    if isinstance(status_message, str) and status_message:
        return status_message
    return "Error"


def _parent_id(data: dict[str, JsonValue], where: str) -> str | None:
    """Return the parent span ID, or ``None`` for a root span, which OTLP sends as an empty string."""
    if data.get("parentSpanId") in (None, ""):
        return None
    return _expect_hex(data, "parentSpanId", where, length=16)


def _key_values(data: dict[str, JsonValue], where: str) -> dict[str, AttributeValue]:
    """Return the OTLP ``attributes`` list of ``data`` as a dict. A missing list means no attributes."""
    attributes: dict[str, AttributeValue] = {}
    for index, item in enumerate(_optional_list(data, "attributes", where)):
        where_item = f"{where}.attributes[{index}]"
        pair = _as_object(item, where_item)
        attributes[_expect_str(pair, "key", where_item)] = _any_value(
            _field(pair, "value", where_item), f"{where_item}.value"
        )
    return attributes


def _any_value(value: JsonValue, where: str) -> AttributeValue:
    """Return an OTLP ``AnyValue`` as an attribute value.

    Arrays, key-value lists and an unset value are stored as their JSON text, because
    attributes hold primitives only. An empty object is an unset value and becomes ``None``.
    """
    data = _as_object(value, where)
    if not data:
        return None
    if "stringValue" in data:
        return _expect_str(data, "stringValue", where)
    if "boolValue" in data:
        flag = _field(data, "boolValue", where)
        if not isinstance(flag, bool):
            raise IngestionError(f"{where}.boolValue must be a boolean")
        return flag
    if "intValue" in data:
        return _int_value(data, "intValue", where)
    if "doubleValue" in data:
        return _expect_number(data, "doubleValue", where)
    if "bytesValue" in data:
        return _expect_str(data, "bytesValue", where)
    if "arrayValue" in data or "kvlistValue" in data:
        return json.dumps(data, sort_keys=True)
    raise IngestionError(
        f"{where} must hold stringValue, boolValue, intValue, doubleValue, "
        "bytesValue, arrayValue or kvlistValue"
    )


def _int_value(data: dict[str, JsonValue], key: str, where: str) -> int:
    """Return the integer stored under ``key``. OTLP JSON sends 64-bit integers as strings."""
    value = _field(data, key, where)
    if isinstance(value, bool):
        raise IngestionError(f"{where}.{key} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value):
        return int(value)
    raise IngestionError(f"{where}.{key} must be an integer, as a string or a number")


def _unix_nanos(data: dict[str, JsonValue], key: str, where: str) -> int:
    """Return the non-negative Unix time in nanoseconds stored under ``key``, sent as a decimal string."""
    value = _field(data, key, where)
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise IngestionError(f"{where}.{key} must be a decimal string of Unix nanoseconds")


def _optional_object(
    data: dict[str, JsonValue], key: str, where: str
) -> dict[str, JsonValue]:
    """Return the object stored under ``key``, or an empty object when it is missing or ``null``."""
    if data.get(key) is None:
        return {}
    return _as_object(data[key], f"{where}.{key}")


def _optional_list(data: dict[str, JsonValue], key: str, where: str) -> list[JsonValue]:
    """Return the array stored under ``key``, or an empty array when it is missing or ``null``.

    OTLP JSON omits empty repeated fields, so a missing array means there are none.
    """
    if data.get(key) is None:
        return []
    return _as_list(data[key], f"{where}.{key}")
