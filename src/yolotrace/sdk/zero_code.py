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


def instrument_installed() -> list[str]:
    """Instrument each supported library that is installed and return the names of those instrumented.

    A library that is not installed is skipped. An integration that fails is reported with a
    warning instead of an exception, so tracing never stops the application from running.
    Calling it again does nothing for libraries that are already instrumented.
    """
    instrumented: list[str] = []
    for name, instrument in _INTEGRATIONS:
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
