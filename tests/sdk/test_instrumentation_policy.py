import dataclasses
from collections.abc import Callable

import pytest

from yolotrace.sdk import zero_code
from yolotrace.sdk.policy import (
    POLICY_VERSION,
    InstrumentationPolicy,
    PolicyError,
    apply_policy,
    load_policy,
)
from yolotrace.sdk.tracing import finished_spans, trace


@pytest.fixture
def fake_integrations(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the real integrations with two fakes that record they ran, so no client is touched."""
    ran: list[str] = []

    def make(name: str) -> Callable[[], None]:
        def instrument() -> None:
            ran.append(name)

        return instrument

    integrations: tuple[tuple[str, Callable[[], None]], ...] = (
        ("openai", make("openai")),
        ("anthropic", make("anthropic")),
    )
    monkeypatch.setattr(zero_code, "_INTEGRATIONS", integrations)
    return ran


def test_empty_document_takes_every_default() -> None:
    assert load_policy("{}") == InstrumentationPolicy()


def test_full_document_sets_every_field() -> None:
    policy = load_policy(
        '{"version": 1, "sample_rate": 0.25, "max_attributes": 8, '
        '"max_attribute_length": 64, "integrations": ["openai"]}'
    )

    assert policy == InstrumentationPolicy(
        version=1,
        sample_rate=0.25,
        max_attributes=8,
        max_attribute_length=64,
        integrations=frozenset({"openai"}),
    )


def test_integer_sample_rate_is_read_as_a_number() -> None:
    assert load_policy('{"sample_rate": 0}').sample_rate == 0.0


def test_policy_is_immutable() -> None:
    policy = InstrumentationPolicy()

    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.sample_rate = 0.5  # type: ignore[misc]


def test_unknown_version_is_rejected() -> None:
    with pytest.raises(PolicyError, match="unsupported policy version 2"):
        load_policy('{"version": 2}')
    with pytest.raises(PolicyError, match="unsupported policy version"):
        InstrumentationPolicy(version=POLICY_VERSION + 1)


def test_unknown_field_is_rejected_instead_of_ignored() -> None:
    with pytest.raises(PolicyError, match="unknown policy fields.*sample_rat"):
        load_policy('{"sample_rat": 0.5}')


def test_invalid_json_is_a_policy_error() -> None:
    with pytest.raises(PolicyError, match="not valid JSON"):
        load_policy("{")


def test_document_that_is_not_an_object_is_rejected() -> None:
    with pytest.raises(PolicyError, match="must be a JSON object"):
        load_policy("[1, 2]")


@pytest.mark.parametrize(
    "document",
    [
        '{"sample_rate": "0.5"}',
        '{"sample_rate": true}',
        '{"max_attributes": 1.5}',
        '{"max_attributes": true}',
        '{"max_attribute_length": "10"}',
        '{"integrations": "openai"}',
        '{"integrations": [1]}',
    ],
)
def test_values_of_the_wrong_type_are_rejected(document: str) -> None:
    with pytest.raises(PolicyError):
        load_policy(document)


@pytest.mark.parametrize("sample_rate", [-0.1, 1.1])
def test_sample_rate_outside_zero_to_one_is_rejected(sample_rate: float) -> None:
    with pytest.raises(PolicyError, match="sample_rate"):
        InstrumentationPolicy(sample_rate=sample_rate)


def test_negative_limits_are_rejected() -> None:
    with pytest.raises(PolicyError, match="cannot be negative"):
        InstrumentationPolicy(max_attributes=-1)
    with pytest.raises(PolicyError, match="cannot be negative"):
        InstrumentationPolicy(max_attribute_length=-1)


def test_unknown_integration_name_is_rejected_when_loading() -> None:
    with pytest.raises(PolicyError, match="unknown integrations: \\['opnai'\\]"):
        load_policy('{"integrations": ["opnai"]}')


def test_apply_sets_recording_settings_and_instruments_every_integration(
    fake_integrations: list[str],
) -> None:
    policy = InstrumentationPolicy(sample_rate=0.0)

    instrumented = apply_policy(policy)

    assert instrumented == ["openai", "anthropic"]
    assert fake_integrations == ["openai", "anthropic"]
    with trace("block"):
        pass
    assert finished_spans() == []


def test_apply_instruments_only_the_integrations_the_policy_names(
    fake_integrations: list[str],
) -> None:
    policy = load_policy('{"integrations": ["anthropic"]}')

    assert apply_policy(policy) == ["anthropic"]
    assert fake_integrations == ["anthropic"]


def test_empty_integration_list_instruments_nothing(
    fake_integrations: list[str],
) -> None:
    assert apply_policy(load_policy('{"integrations": []}')) == []
    assert fake_integrations == []


def test_apply_replaces_the_settings_of_an_earlier_policy(
    fake_integrations: list[str],
) -> None:
    apply_policy(InstrumentationPolicy(max_attributes=0))

    apply_policy(InstrumentationPolicy())

    with trace("block", attributes={"key": "value"}) as span:
        pass
    assert span.attributes == {"key": "value"}
