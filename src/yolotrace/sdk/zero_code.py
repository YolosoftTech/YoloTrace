"""Turns on instrumentation for every supported LLM library that is installed, without code changes."""

from __future__ import annotations

import warnings
from collections.abc import Callable

from yolotrace.sdk.instrumentation import (
    instrument_anthropic,
    instrument_langchain,
    instrument_openai,
    instrument_openai_agents,
)

_INTEGRATIONS: tuple[tuple[str, Callable[[], None]], ...] = (
    ("openai", instrument_openai),
    ("anthropic", instrument_anthropic),
    ("langchain", instrument_langchain),
    ("openai-agents", instrument_openai_agents),
)


def supported_integrations() -> frozenset[str]:
    """Return the names of every integration that ``instrument_installed`` knows about."""
    return frozenset(name for name, _ in _INTEGRATIONS)


def instrument_installed(integrations: frozenset[str] | None = None) -> list[str]:
    """Instrument each supported library that is installed and return the names of those instrumented.

    ``integrations`` limits the run to the named integrations; ``None`` tries all of them.
    A library that is not installed is skipped. An integration that fails is reported with a
    warning instead of an exception, so tracing never stops the application from running.
    Calling it again does nothing for libraries that are already instrumented.
    """
    instrumented: list[str] = []
    for name, instrument in _INTEGRATIONS:
        if integrations is not None and name not in integrations:
            continue
        try:
            instrument()
        except ImportError:
            continue
        except Exception as exc:  # noqa: BLE001 - tracing must never stop the application
            warnings.warn(
                f"YoloTrace could not instrument {name!r}: {exc}", stacklevel=2
            )
            continue
        instrumented.append(name)
    return instrumented
