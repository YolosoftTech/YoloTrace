"""Validate JSON trace batches received by the ingestion API and turn them into spans."""

from __future__ import annotations

import json
import re
from typing import get_args

from yolotrace.sdk.export import SCHEMA_VERSION, JsonValue
from yolotrace.sdk.tracing import AttributeValue, Span, SpanEvent, SpanKind

_SPAN_KINDS: tuple[SpanKind, ...] = get_args(SpanKind)
_SPAN_STATUSES = ("ok", "error")


class IngestionError(ValueError):
    """Raised when a batch is not valid JSON in the documented format. The message names the invalid field."""


def parse_batch(body: bytes) -> list[Span]:
    """Parse a JSON batch body into spans, in the order they were sent.

    Raises ``IngestionError`` naming the first invalid part of the batch, so the sender
    can fix it. Nothing is returned for a batch that is only partly valid.
    """
    try:
        document: JsonValue = json.loads(body)
    except ValueError as error:
        raise IngestionError(f"body is not valid JSON: {error}") from error
    batch = _as_object(document, "body")
    version = _expect_int(batch, "schema_version", "body")
    if version != SCHEMA_VERSION:
        raise IngestionError(
            f"body.schema_version must be {SCHEMA_VERSION}, got {version}"
        )
    items = _as_list(_field(batch, "spans", "body"), "body.spans")
    return [
        span_from_dict(_as_object(item, f"body.spans[{index}]"), f"body.spans[{index}]")
        for index, item in enumerate(items)
    ]


def span_from_dict(data: dict[str, JsonValue], where: str) -> Span:
    """Build a span from one object of the JSON format. ``where`` names the object in error messages."""
    return Span(
        name=_expect_str(data, "name", where),
        trace_id=_expect_hex(data, "trace_id", where, length=32),
        span_id=_expect_hex(data, "span_id", where, length=16),
        parent_id=_optional_hex(data, "parent_id", where, length=16),
        start_time=_expect_number(data, "start_time", where),
        end_time=_optional_number(data, "end_time", where),
        status=_expect_choice(data, "status", where, _SPAN_STATUSES),
        error=_optional_str(data, "error", where),
        kind=_expect_span_kind(data, "kind", where),
        attributes=_expect_attributes(data, "attributes", where),
        events=_expect_events(data, "events", where),
        sampled=_expect_bool(data, "sampled", where),
        _duration=_optional_number(data, "duration", where),
    )


def _expect_span_kind(data: dict[str, JsonValue], key: str, where: str) -> SpanKind:
    """Return the span kind stored under ``key``, which must be one of the kinds the SDK records."""
    value = _expect_str(data, key, where)
    for kind in _SPAN_KINDS:
        if value == kind:
            return kind
    raise IngestionError(f"{where}.{key} must be one of {', '.join(_SPAN_KINDS)}")


def _expect_events(data: dict[str, JsonValue], key: str, where: str) -> list[SpanEvent]:
    """Return the events stored under ``key``, each with a name, a time and attributes."""
    where_events = f"{where}.{key}"
    events: list[SpanEvent] = []
    for index, item in enumerate(_as_list(_field(data, key, where), where_events)):
        where_event = f"{where_events}[{index}]"
        event = _as_object(item, where_event)
        events.append(
            SpanEvent(
                name=_expect_str(event, "name", where_event),
                time=_expect_number(event, "time", where_event),
                attributes=_expect_attributes(event, "attributes", where_event),
            )
        )
    return events


def _expect_attributes(
    data: dict[str, JsonValue], key: str, where: str
) -> dict[str, AttributeValue]:
    """Return the attribute object stored under ``key``. Values must be primitives, not lists or objects."""
    where_attributes = f"{where}.{key}"
    attributes: dict[str, AttributeValue] = {}
    for name, value in _as_object(_field(data, key, where), where_attributes).items():
        if value is None or isinstance(value, str | int | float | bool):
            attributes[name] = value
        else:
            raise IngestionError(
                f"{where_attributes}.{name} must be a string, number, boolean or null"
            )
    return attributes


def _as_object(value: JsonValue, where: str) -> dict[str, JsonValue]:
    """Return ``value`` when it is a JSON object, or raise ``IngestionError`` naming ``where``."""
    if not isinstance(value, dict):
        raise IngestionError(f"{where} must be a JSON object")
    return value


def _as_list(value: JsonValue, where: str) -> list[JsonValue]:
    """Return ``value`` when it is a JSON array, or raise ``IngestionError`` naming ``where``."""
    if not isinstance(value, list):
        raise IngestionError(f"{where} must be a JSON array")
    return value


def _field(data: dict[str, JsonValue], key: str, where: str) -> JsonValue:
    """Return the value stored under ``key``, or raise ``IngestionError`` when it is missing."""
    if key not in data:
        raise IngestionError(f"{where}.{key} is required")
    return data[key]


def _expect_str(data: dict[str, JsonValue], key: str, where: str) -> str:
    """Return the string stored under ``key``."""
    value = _field(data, key, where)
    if not isinstance(value, str):
        raise IngestionError(f"{where}.{key} must be a string")
    return value


def _optional_str(data: dict[str, JsonValue], key: str, where: str) -> str | None:
    """Return the string stored under ``key``, or ``None`` when it is ``null``."""
    if _field(data, key, where) is None:
        return None
    return _expect_str(data, key, where)


def _expect_choice(
    data: dict[str, JsonValue], key: str, where: str, choices: tuple[str, ...]
) -> str:
    """Return the string stored under ``key`` when it is one of ``choices``."""
    value = _expect_str(data, key, where)
    if value not in choices:
        raise IngestionError(f"{where}.{key} must be one of {', '.join(choices)}")
    return value


def _expect_hex(
    data: dict[str, JsonValue], key: str, where: str, *, length: int
) -> str:
    """Return the lowercase hexadecimal string stored under ``key``, which must be ``length`` characters."""
    value = _expect_str(data, key, where)
    if re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is None:
        raise IngestionError(
            f"{where}.{key} must be {length} lowercase hexadecimal characters"
        )
    return value


def _optional_hex(
    data: dict[str, JsonValue], key: str, where: str, *, length: int
) -> str | None:
    """Return the hexadecimal string stored under ``key``, or ``None`` when it is ``null``."""
    if _field(data, key, where) is None:
        return None
    return _expect_hex(data, key, where, length=length)


def _expect_number(data: dict[str, JsonValue], key: str, where: str) -> float:
    """Return the number stored under ``key`` as a float. Booleans are not numbers here."""
    value = _field(data, key, where)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise IngestionError(f"{where}.{key} must be a number")
    return float(value)


def _optional_number(data: dict[str, JsonValue], key: str, where: str) -> float | None:
    """Return the number stored under ``key``, or ``None`` when it is ``null``."""
    if _field(data, key, where) is None:
        return None
    return _expect_number(data, key, where)


def _expect_bool(data: dict[str, JsonValue], key: str, where: str) -> bool:
    """Return the boolean stored under ``key``."""
    value = _field(data, key, where)
    if not isinstance(value, bool):
        raise IngestionError(f"{where}.{key} must be a boolean")
    return value


def _expect_int(data: dict[str, JsonValue], key: str, where: str) -> int:
    """Return the integer stored under ``key``. Booleans are not integers here."""
    value = _field(data, key, where)
    if isinstance(value, bool) or not isinstance(value, int):
        raise IngestionError(f"{where}.{key} must be an integer")
    return value
