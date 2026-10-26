from collections.abc import Callable

import pytest
from anthropic.resources.messages import AsyncMessages, Messages
from openai.resources.chat.completions import AsyncCompletions, Completions

from yolotrace.sdk import zero_code
from yolotrace.sdk.llm_calls import is_wrapped


@pytest.fixture(autouse=True)
def restore_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the wrapping that instrumenting the installed clients applies, after each test."""
    for target, name in (
        (Completions, "create"),
        (AsyncCompletions, "create"),
        (Messages, "create"),
        (AsyncMessages, "create"),
    ):
        monkeypatch.setattr(target, name, getattr(target, name))


def test_installed_llm_clients_are_instrumented() -> None:
    instrumented = zero_code.instrument_installed()

    assert "openai" in instrumented
    assert "anthropic" in instrumented
    assert is_wrapped(Completions.create)
    assert is_wrapped(Messages.create)


def test_calling_it_again_keeps_each_client_wrapped_once() -> None:
    zero_code.instrument_installed()
    first_wrapper = Completions.create

    instrumented = zero_code.instrument_installed()

    assert "openai" in instrumented
    assert Completions.create is first_wrapper


def test_missing_library_is_skipped_without_a_warning(
    monkeypatch: pytest.MonkeyPatch, recwarn: pytest.WarningsRecorder
) -> None:
    def missing() -> None:
        raise ImportError("requires the 'missing' package")

    def available() -> None:
        pass

    monkeypatch.setattr(
        zero_code,
        "_INTEGRATIONS",
        (("missing", missing), ("available", available)),
    )

    assert zero_code.instrument_installed() == ["available"]
    assert len(recwarn) == 0


def test_failing_integration_warns_and_the_others_still_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken() -> None:
        raise RuntimeError("unsupported version")

    def available() -> None:
        pass

    integrations: tuple[tuple[str, Callable[[], None]], ...] = (
        ("broken", broken),
        ("available", available),
    )
    monkeypatch.setattr(zero_code, "_INTEGRATIONS", integrations)

    with pytest.warns(UserWarning, match="could not instrument 'broken'"):
        instrumented = zero_code.instrument_installed()

    assert instrumented == ["available"]


def test_only_the_named_integrations_are_instrumented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def openai() -> None:
        pass

    def anthropic() -> None:
        pass

    monkeypatch.setattr(
        zero_code,
        "_INTEGRATIONS",
        (("openai", openai), ("anthropic", anthropic)),
    )

    assert zero_code.instrument_installed(frozenset({"anthropic"})) == ["anthropic"]
    assert zero_code.supported_integrations() == frozenset({"openai", "anthropic"})
