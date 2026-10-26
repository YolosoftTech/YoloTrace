"""Versioned instrumentation policies that a deployment can keep in a file and apply in one call."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from yolotrace.sdk.tracing import configure
from yolotrace.sdk.zero_code import instrument_installed, supported_integrations

POLICY_VERSION = 1
"""The only policy format this SDK reads. A policy with another ``version`` is rejected."""

_DEFAULT_MAX_ATTRIBUTES = 128
_DEFAULT_MAX_ATTRIBUTE_LENGTH = 4096
_FIELDS = frozenset(
    {
        "version",
        "sample_rate",
        "max_attributes",
        "max_attribute_length",
        "integrations",
    }
)

PolicyValue = str | int | float | bool | list[str] | None
"""A value a policy document may hold for one of its fields."""


class PolicyError(ValueError):
    """Raised when a policy is malformed, uses an unknown version or field, or names an unknown integration."""


@dataclass(frozen=True, slots=True)
class InstrumentationPolicy:
    """The recording settings and integrations that one deployment should use.

    ``sample_rate``, ``max_attributes`` and ``max_attribute_length`` mean what they mean in
    ``configure``. ``integrations`` names the integrations to enable; ``None`` enables every
    supported integration that is installed. Invalid values raise ``PolicyError`` as soon as
    the policy is built, so a bad policy never half-applies.
    """

    version: int = POLICY_VERSION
    sample_rate: float = 1.0
    max_attributes: int = _DEFAULT_MAX_ATTRIBUTES
    max_attribute_length: int = _DEFAULT_MAX_ATTRIBUTE_LENGTH
    integrations: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if self.version != POLICY_VERSION:
            raise PolicyError(
                f"unsupported policy version {self.version!r}; this SDK reads version {POLICY_VERSION}"
            )
        if not 0.0 <= self.sample_rate <= 1.0:
            raise PolicyError(
                f"sample_rate must be between 0.0 and 1.0: {self.sample_rate!r}"
            )
        if self.max_attributes < 0 or self.max_attribute_length < 0:
            raise PolicyError(
                "max_attributes and max_attribute_length cannot be negative"
            )
        if self.integrations is not None:
            unknown = self.integrations - supported_integrations()
            if unknown:
                raise PolicyError(f"unknown integrations: {sorted(unknown)}")


def load_policy(document: str) -> InstrumentationPolicy:
    """Parse a JSON policy document into an ``InstrumentationPolicy``.

    Fields left out take their defaults. An unknown field is rejected rather than ignored,
    so a misspelled setting cannot silently do nothing. Raises ``PolicyError`` when the
    document is not a JSON object, has an unknown field, or has a value of the wrong type.
    """
    try:
        data = json.loads(document)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"policy is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError("policy must be a JSON object")

    unknown = sorted(str(key) for key in data.keys() - _FIELDS)
    if unknown:
        raise PolicyError(f"unknown policy fields: {unknown}")

    return InstrumentationPolicy(
        version=_int_field(data, "version", POLICY_VERSION),
        sample_rate=_float_field(data, "sample_rate", 1.0),
        max_attributes=_int_field(data, "max_attributes", _DEFAULT_MAX_ATTRIBUTES),
        max_attribute_length=_int_field(
            data, "max_attribute_length", _DEFAULT_MAX_ATTRIBUTE_LENGTH
        ),
        integrations=_integrations_field(data),
    )


def apply_policy(policy: InstrumentationPolicy) -> list[str]:
    """Apply ``policy`` to this process and return the names of the integrations instrumented.

    The recording settings are set the same way ``configure`` sets them, and then the
    policy's integrations are instrumented the same way ``instrument_installed`` does it.
    Calling it again replaces the settings and keeps already-instrumented libraries as they are.
    """
    configure(
        sample_rate=policy.sample_rate,
        max_attributes=policy.max_attributes,
        max_attribute_length=policy.max_attribute_length,
    )
    return instrument_installed(policy.integrations)


def _int_field(data: Mapping[str, PolicyValue], key: str, default: int) -> int:
    """Return the integer stored under ``key``, or ``default`` when the document leaves it out."""
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError(f"{key} must be an integer")
    return value


def _float_field(data: Mapping[str, PolicyValue], key: str, default: float) -> float:
    """Return the number stored under ``key`` as a float, or ``default`` when it is left out."""
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PolicyError(f"{key} must be a number")
    return float(value)


def _integrations_field(data: Mapping[str, PolicyValue]) -> frozenset[str] | None:
    """Return the integration names stored under ``integrations``, or ``None`` for all of them."""
    value = data.get("integrations")
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise PolicyError("integrations must be a list of integration names")
    return frozenset(value)
