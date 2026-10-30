import json

import pytest

from yolotrace.ingestion.parsing import IngestionError, parse_batch
from yolotrace.sdk.export import SCHEMA_VERSION, JsonValue, span_to_dict
from yolotrace.sdk.tracing import Span, SpanEvent

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
SPAN_ID = "b7ad6b7169203331"


def _span_json(**overrides: JsonValue) -> dict[str, JsonValue]:
    """Return one span in the JSON format, with ``overrides`` applied on top of a valid span."""
    span: dict[str, JsonValue] = {
        "name": "answer",
        "trace_id": TRACE_ID,
        "span_id": SPAN_ID,
        "parent_id": None,
        "kind": "run",
        "start_time": 1000.5,
        "end_time": 1001.0,
        "duration": 0.5,
        "status": "ok",
        "error": None,
        "attributes": {"model": "gpt-test", "tokens": 12, "cached": False},
        "events": [{"name": "retry", "time": 1000.6, "attributes": {"attempt": 2}}],
        "sampled": True,
    }
    span.update(overrides)
    return span


def _batch(*spans: dict[str, JsonValue]) -> bytes:
    """Return a batch body holding ``spans`` in the documented format."""
    return json.dumps({"schema_version": SCHEMA_VERSION, "spans": list(spans)}).encode()


def test_valid_batch_becomes_spans_in_the_order_sent() -> None:
    first = _span_json(name="first")
    second = _span_json(name="second", span_id="1111111111111111")

    spans = parse_batch(_batch(first, second))

    assert [span.name for span in spans] == ["first", "second"]
    assert all(isinstance(span, Span) for span in spans)


def test_parsed_span_keeps_every_field() -> None:
    (span,) = parse_batch(_batch(_span_json(parent_id=SPAN_ID, error=None)))

    assert span.trace_id == TRACE_ID
    assert span.span_id == SPAN_ID
    assert span.parent_id == SPAN_ID
    assert span.kind == "run"
    assert span.start_time == 1000.5
    assert span.end_time == 1001.0
    assert span.duration == 0.5
    assert span.status == "ok"
    assert span.error is None
    assert span.attributes == {"model": "gpt-test", "tokens": 12, "cached": False}
    assert span.events == [
        SpanEvent(name="retry", time=1000.6, attributes={"attempt": 2})
    ]
    assert span.sampled is True


def test_running_span_with_null_end_and_duration_is_accepted() -> None:
    (span,) = parse_batch(_batch(_span_json(end_time=None, duration=None)))

    assert span.end_time is None
    assert span.duration is None


def test_failed_span_keeps_status_and_error() -> None:
    (span,) = parse_batch(
        _batch(_span_json(status="error", error="ValueError: bad input"))
    )

    assert span.status == "error"
    assert span.error == "ValueError: bad input"


def test_span_exported_by_the_sdk_parses_back_to_the_same_fields() -> None:
    original = Span(
        name="llm",
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        parent_id=None,
        start_time=5.0,
        end_time=6.0,
        kind="llm",
        attributes={"provider": "openai", "tokens": 7},
        events=[SpanEvent(name="chunk", time=5.5, attributes={"index": 1})],
        _duration=1.0,
    )
    body = json.dumps(
        {"schema_version": SCHEMA_VERSION, "spans": [span_to_dict(original)]}
    )

    (parsed,) = parse_batch(body.encode())

    assert span_to_dict(parsed) == span_to_dict(original)


def test_empty_batch_is_valid() -> None:
    assert parse_batch(_batch()) == []


@pytest.mark.parametrize(
    "body",
    [b"not json", b"\xff\xfe", b"[]", b'"text"'],
    ids=["invalid-json", "invalid-utf8", "array", "string"],
)
def test_body_that_is_not_a_json_object_is_rejected(body: bytes) -> None:
    with pytest.raises(IngestionError, match="body"):
        parse_batch(body)


def test_wrong_schema_version_is_rejected() -> None:
    body = json.dumps({"schema_version": SCHEMA_VERSION + 1, "spans": []}).encode()

    with pytest.raises(IngestionError, match="body.schema_version must be 1, got 2"):
        parse_batch(body)


def test_boolean_schema_version_is_rejected() -> None:
    body = json.dumps({"schema_version": True, "spans": []}).encode()

    with pytest.raises(IngestionError, match="schema_version must be an integer"):
        parse_batch(body)


def test_missing_spans_list_is_rejected() -> None:
    body = json.dumps({"schema_version": SCHEMA_VERSION}).encode()

    with pytest.raises(IngestionError, match="body.spans is required"):
        parse_batch(body)


def test_spans_that_is_not_a_list_is_rejected() -> None:
    body = json.dumps({"schema_version": SCHEMA_VERSION, "spans": {}}).encode()

    with pytest.raises(IngestionError, match="body.spans must be a JSON array"):
        parse_batch(body)


def test_span_that_is_not_an_object_names_its_position() -> None:
    body = json.dumps(
        {"schema_version": SCHEMA_VERSION, "spans": [_span_json(), "text"]}
    ).encode()

    with pytest.raises(IngestionError, match=r"body.spans\[1\] must be a JSON object"):
        parse_batch(body)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", 3, "name must be a string"),
        ("trace_id", "ABC", "trace_id must be 32 lowercase hexadecimal characters"),
        ("trace_id", TRACE_ID[:-1], "trace_id must be 32 lowercase"),
        ("span_id", "zz" * 8, "span_id must be 16 lowercase"),
        ("parent_id", "1234", "parent_id must be 16 lowercase"),
        ("kind", "pipeline", "kind must be one of run, step, llm, tool"),
        ("status", "unknown", "status must be one of ok, error"),
        ("start_time", "now", "start_time must be a number"),
        ("start_time", True, "start_time must be a number"),
        ("end_time", "later", "end_time must be a number"),
        ("duration", False, "duration must be a number"),
        ("error", 1, "error must be a string"),
        ("sampled", "yes", "sampled must be a boolean"),
        ("attributes", [], "attributes must be a JSON object"),
        ("attributes", {"nested": {"a": 1}}, "attributes.nested must be a string"),
        ("attributes", {"items": [1]}, "attributes.items must be a string"),
        ("events", {}, "events must be a JSON array"),
        ("events", ["retry"], r"events\[0\] must be a JSON object"),
    ],
)
def test_invalid_span_field_is_rejected_with_its_name(
    field: str, value: JsonValue, message: str
) -> None:
    with pytest.raises(IngestionError, match=rf"body.spans\[0\]\.{message}"):
        parse_batch(_batch(_span_json(**{field: value})))


def test_missing_span_field_is_rejected_with_its_name() -> None:
    span = _span_json()
    del span["span_id"]

    with pytest.raises(IngestionError, match=r"body.spans\[0\].span_id is required"):
        parse_batch(_batch(span))


def test_invalid_event_is_rejected_with_its_position() -> None:
    event: dict[str, JsonValue] = {"name": "retry", "time": "soon", "attributes": {}}

    with pytest.raises(
        IngestionError, match=r"body.spans\[0\].events\[0\].time must be a number"
    ):
        parse_batch(_batch(_span_json(events=[event])))


def test_invalid_attribute_names_its_key() -> None:
    with pytest.raises(
        IngestionError, match=r"body.spans\[0\].attributes.tags must be a string"
    ):
        parse_batch(_batch(_span_json(attributes={"tags": ["a"]})))


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_non_finite_number_is_rejected(constant: bytes) -> None:
    body = _batch(_span_json()).replace(
        b'"start_time": 1000.5', b'"start_time": ' + constant
    )

    with pytest.raises(IngestionError, match="is not a valid JSON number"):
        parse_batch(body)


def test_span_that_ends_before_it_starts_is_rejected() -> None:
    with pytest.raises(
        IngestionError,
        match=r"body.spans\[0\].end_time must not be before body.spans\[0\].start_time",
    ):
        parse_batch(_batch(_span_json(start_time=5.0, end_time=4.0)))


def test_span_that_ends_when_it_starts_is_accepted() -> None:
    (span,) = parse_batch(_batch(_span_json(start_time=5.0, end_time=5.0)))

    assert span.end_time == span.start_time


def test_body_nested_too_deeply_is_rejected_not_crashed() -> None:
    body = b'{"schema_version": 1, "spans": ' + b"[" * 100_000 + b"]" * 100_000 + b"}"

    with pytest.raises(IngestionError, match="nested too deeply"):
        parse_batch(body)
