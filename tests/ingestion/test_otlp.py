import json
import re

import pytest

from yolotrace.ingestion.otlp import parse_otlp_request
from yolotrace.ingestion.parsing import IngestionError
from yolotrace.sdk.export import JsonValue
from yolotrace.sdk.tracing import Span

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
SPAN_ID = "b7ad6b7169203331"
CHILD_ID = "1111111111111111"


def _span_json(**overrides: JsonValue) -> dict[str, JsonValue]:
    """Return one OTLP span with ``overrides`` applied on top of a valid root span."""
    span: dict[str, JsonValue] = {
        "traceId": TRACE_ID,
        "spanId": SPAN_ID,
        "parentSpanId": "",
        "name": "answer",
        "startTimeUnixNano": "1000500000000",
        "endTimeUnixNano": "1001000000000",
        "attributes": [],
        "status": {"code": 1},
    }
    span.update(overrides)
    return span


def _request(*spans: dict[str, JsonValue], resource: JsonValue | None = None) -> bytes:
    """Return an OTLP/HTTP JSON request holding ``spans`` under one resource."""
    resource_span: dict[str, JsonValue] = {
        "scopeSpans": [{"scope": {"name": "test"}, "spans": list(spans)}]
    }
    if resource is not None:
        resource_span["resource"] = resource
    return json.dumps({"resourceSpans": [resource_span]}).encode()


def _attribute(key: str, value: JsonValue) -> dict[str, JsonValue]:
    """Return one OTLP ``KeyValue`` for ``key``."""
    return {"key": key, "value": value}


def test_empty_request_has_no_spans() -> None:
    assert parse_otlp_request(b"{}") == []
    assert parse_otlp_request(b'{"resourceSpans": []}') == []


def test_span_keeps_ids_name_and_times_in_seconds() -> None:
    (span,) = parse_otlp_request(_request(_span_json()))

    assert isinstance(span, Span)
    assert span.trace_id == TRACE_ID
    assert span.span_id == SPAN_ID
    assert span.parent_id is None
    assert span.name == "answer"
    assert span.start_time == 1000.5
    assert span.end_time == 1001.0
    assert span.duration == 0.5
    assert span.sampled is True


def test_child_span_keeps_its_parent() -> None:
    (span,) = parse_otlp_request(_request(_span_json(parentSpanId=SPAN_ID)))

    assert span.parent_id == SPAN_ID


def test_spans_keep_the_order_they_were_sent_in() -> None:
    spans = parse_otlp_request(
        _request(_span_json(name="first"), _span_json(name="second", spanId=CHILD_ID))
    )

    assert [span.name for span in spans] == ["first", "second"]


def test_resource_attributes_are_copied_and_span_attributes_win() -> None:
    resource: JsonValue = {
        "attributes": [
            _attribute("service.name", {"stringValue": "billing"}),
            _attribute("env", {"stringValue": "dev"}),
        ]
    }
    span = _span_json(
        attributes=[_attribute("env", {"stringValue": "prod"})],
    )

    (parsed,) = parse_otlp_request(_request(span, resource=resource))

    assert parsed.attributes == {"service.name": "billing", "env": "prod"}


def test_attribute_values_keep_their_primitive_types() -> None:
    span = _span_json(
        attributes=[
            _attribute("text", {"stringValue": "hi"}),
            _attribute("flag", {"boolValue": True}),
            _attribute("count", {"intValue": "12"}),
            _attribute("ratio", {"doubleValue": 0.25}),
            _attribute("blob", {"bytesValue": "AQI="}),
            _attribute("unset", {}),
        ]
    )

    (parsed,) = parse_otlp_request(_request(span))

    assert parsed.attributes == {
        "text": "hi",
        "flag": True,
        "count": 12,
        "ratio": 0.25,
        "blob": "AQI=",
        "unset": None,
    }


def test_array_and_key_value_list_attributes_are_stored_as_json_text() -> None:
    span = _span_json(
        attributes=[
            _attribute(
                "tags",
                {"arrayValue": {"values": [{"stringValue": "a"}]}},
            ),
        ]
    )

    (parsed,) = parse_otlp_request(_request(span))

    assert parsed.attributes["tags"] == json.dumps(
        {"arrayValue": {"values": [{"stringValue": "a"}]}}, sort_keys=True
    )


def test_events_keep_their_name_time_and_attributes() -> None:
    span = _span_json(
        events=[
            {
                "name": "retry",
                "timeUnixNano": "1000600000000",
                "attributes": [_attribute("attempt", {"intValue": "2"})],
            }
        ]
    )

    (parsed,) = parse_otlp_request(_request(span))

    (event,) = parsed.events
    assert event.name == "retry"
    assert event.time == 1000.6
    assert event.attributes == {"attempt": 2}


def test_ok_status_is_not_an_error() -> None:
    (span,) = parse_otlp_request(
        _request(_span_json(status={"code": "STATUS_CODE_OK"}))
    )

    assert span.status == "ok"
    assert span.error is None


def test_unset_status_is_not_an_error() -> None:
    (span,) = parse_otlp_request(_request(_span_json(status={})))

    assert span.status == "ok"
    assert span.error is None


@pytest.mark.parametrize("code", [2, "STATUS_CODE_ERROR"])
def test_error_status_takes_the_text_of_the_exception_event(code: JsonValue) -> None:
    span = _span_json(
        status={"code": code, "message": "fallback"},
        events=[
            {
                "name": "exception",
                "timeUnixNano": "1000900000000",
                "attributes": [
                    _attribute("exception.type", {"stringValue": "ValueError"}),
                    _attribute("exception.message", {"stringValue": "boom"}),
                ],
            }
        ],
    )

    (parsed,) = parse_otlp_request(_request(span))

    assert parsed.status == "error"
    assert parsed.error == "ValueError: boom"


def test_error_status_without_exception_uses_the_status_message() -> None:
    (parsed,) = parse_otlp_request(
        _request(_span_json(status={"code": 2, "message": "upstream timed out"}))
    )

    assert parsed.status == "error"
    assert parsed.error == "upstream timed out"


def test_error_status_without_any_text_reads_error() -> None:
    (parsed,) = parse_otlp_request(_request(_span_json(status={"code": 2})))

    assert parsed.error == "Error"


@pytest.mark.parametrize(
    ("operation", "parent", "expected"),
    [
        ("execute_tool", SPAN_ID, "tool"),
        ("invoke_agent", None, "run"),
        ("chat", SPAN_ID, "llm"),
        ("text_completion", None, "llm"),
        ("unknown", SPAN_ID, "step"),
        ("unknown", None, "run"),
    ],
)
def test_genai_operation_decides_the_span_kind(
    operation: str, parent: str | None, expected: str
) -> None:
    span = _span_json(
        parentSpanId=parent or "",
        attributes=[_attribute("gen_ai.operation.name", {"stringValue": operation})],
    )

    (parsed,) = parse_otlp_request(_request(span))

    assert parsed.kind == expected


def test_model_attribute_marks_a_span_as_an_llm_call() -> None:
    span = _span_json(
        attributes=[_attribute("gen_ai.request.model", {"stringValue": "gpt-test"})]
    )

    (parsed,) = parse_otlp_request(_request(span))

    assert parsed.kind == "llm"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"traceId": "XYZ"}, "spans[0].traceId must be 32"),
        ({"spanId": "xyz"}, "spans[0].spanId must be 16"),
        ({"parentSpanId": "short"}, "spans[0].parentSpanId must be 16"),
        ({"name": None}, "spans[0].name must be a string"),
        ({"startTimeUnixNano": 1000}, "spans[0].startTimeUnixNano must be a decimal"),
        ({"endTimeUnixNano": "-5"}, "spans[0].endTimeUnixNano must be a decimal"),
        ({"status": {"code": 9}}, "spans[0].status.code must be one of"),
        ({"status": {"code": True}}, "spans[0].status.code must be one of"),
        ({"events": [{"name": "x"}]}, "spans[0].events[0].timeUnixNano is required"),
        (
            {"attributes": [_attribute("bad", {"stringValue": 1})]},
            "spans[0].attributes[0].value.stringValue must be a string",
        ),
        (
            {"attributes": [_attribute("bad", {"intValue": True})]},
            "spans[0].attributes[0].value.intValue must be an integer",
        ),
        (
            {"attributes": [_attribute("bad", {"mystery": 1})]},
            "spans[0].attributes[0].value must hold",
        ),
        (
            {"endTimeUnixNano": "1000000000000"},
            "spans[0].endTimeUnixNano must not be before",
        ),
    ],
)
def test_invalid_span_is_refused_with_the_field_named(
    overrides: dict[str, JsonValue], message: str
) -> None:
    span = _span_json(**overrides)

    with pytest.raises(IngestionError, match=re.escape(message)):
        parse_otlp_request(_request(span))


def test_one_invalid_span_refuses_the_whole_request() -> None:
    bad = _span_json(name=3)

    with pytest.raises(
        IngestionError, match=re.escape("spans[2].name must be a string")
    ):
        parse_otlp_request(_request(_span_json(), _span_json(), bad))


def test_body_that_is_not_json_is_refused() -> None:
    with pytest.raises(IngestionError, match="body is not valid JSON"):
        parse_otlp_request(b"{not json")


def test_body_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(IngestionError, match="body must be a JSON object"):
        parse_otlp_request(b"[]")


def test_non_finite_attribute_number_is_refused() -> None:
    span = _span_json(attributes=[_attribute("score", {"doubleValue": 0})])
    request = _request(span).replace(b'"doubleValue": 0', b'"doubleValue": NaN')

    with pytest.raises(IngestionError, match="is not a valid JSON number"):
        parse_otlp_request(request)
