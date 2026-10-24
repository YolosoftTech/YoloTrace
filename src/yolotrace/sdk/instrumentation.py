"""Optional instrumentation that records LLM client calls as spans."""

from __future__ import annotations

import importlib


def instrument_openai() -> None:
    """Record each ``chat.completions.create`` call of the ``openai`` package as an LLM span.

    Sync and async clients are both covered. A streaming call (``stream=True``) is
    recorded as one span that ends when the stream is exhausted or closed.
    """
    _require_package(
        "openai.resources.chat.completions", "instrument_openai()", "openai"
    )
    from yolotrace.sdk.openai_integration import install

    install()


def instrument_anthropic() -> None:
    """Record each ``messages.create`` call of the ``anthropic`` package as an LLM span.

    Sync and async clients are both covered. A streaming call (``stream=True``) is
    recorded as one span that ends when the stream is exhausted or closed.
    """
    _require_package(
        "anthropic.resources.messages", "instrument_anthropic()", "anthropic"
    )
    from yolotrace.sdk.anthropic_integration import install

    install()


def instrument_langchain() -> None:
    """Record every LangChain run in the process as spans: chains, chat/LLM calls and tools."""
    try:
        from yolotrace.sdk.langchain_integration import install
    except ImportError as exc:
        raise ImportError(
            "instrument_langchain() requires the 'langchain-core' package"
        ) from exc

    install()


def instrument_openai_agents() -> None:
    """Record every OpenAI Agents SDK trace in the process: agents, model calls, tools and handoffs."""
    try:
        from yolotrace.sdk.openai_agents_integration import install
    except ImportError as exc:
        raise ImportError(
            "instrument_openai_agents() requires the 'openai-agents' package"
        ) from exc

    install()


def _require_package(module: str, caller: str, package: str) -> None:
    """Raise a clear ImportError when ``module`` cannot be imported from ``package``."""
    try:
        importlib.import_module(module)
    except ImportError as exc:
        raise ImportError(f"{caller} requires the '{package}' package") from exc
