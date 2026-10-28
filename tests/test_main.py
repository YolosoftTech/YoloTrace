import sys
from pathlib import Path

import pytest
from openai.resources.chat.completions import AsyncCompletions, Completions

from yolotrace.__main__ import main
from yolotrace.sdk import tracing
from yolotrace.sdk.llm_calls import is_wrapped


@pytest.fixture(autouse=True)
def restore_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test with no recorded spans, and undo the wrapping and ``sys.argv`` changes ``run`` makes."""
    tracing._finished.clear()
    tracing._running.clear()
    monkeypatch.setattr(Completions, "create", Completions.create)
    monkeypatch.setattr(AsyncCompletions, "create", AsyncCompletions.create)
    monkeypatch.setattr(sys, "argv", sys.argv)


def test_run_executes_script_with_its_arguments_and_instruments_openai(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "app.py"
    script.write_text(
        "import sys\n"
        "from yolotrace.sdk.llm_calls import is_wrapped\n"
        "from openai.resources.chat.completions import Completions\n"
        "print(__name__, sys.argv[1:])\n"
        "print(is_wrapped(Completions.create))\n",
        encoding="utf-8",
    )

    exit_code = main(["run", str(script), "--model", "gpt-test"])

    assert exit_code == 0
    assert is_wrapped(Completions.create)
    assert "__main__ ['--model', 'gpt-test']" in capsys.readouterr().out


def test_run_prints_summary_of_recorded_spans_and_llm_calls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "app.py"
    script.write_text(
        "from yolotrace import start_span\n"
        'span = start_span("call", kind="llm")\n'
        'span.end(RuntimeError("rate limited"))\n',
        encoding="utf-8",
    )

    main(["run", str(script)])

    assert (
        "YoloTrace: recorded 1 spans, 1 LLM calls, 1 failed" in capsys.readouterr().err
    )


def test_run_prints_summary_even_when_script_raises(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "app.py"
    script.write_text(
        "from yolotrace import trace\n"
        'with trace("step"):\n'
        "    pass\n"
        'raise RuntimeError("boom")\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="boom"):
        main(["run", str(script)])

    assert (
        "YoloTrace: recorded 1 spans, 0 LLM calls, 0 failed" in capsys.readouterr().err
    )


def test_run_requires_a_command() -> None:
    with pytest.raises(SystemExit):
        main([])


def test_serve_runs_the_ingestion_api_on_the_given_address(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "yolotrace.__main__.run_ingestion_server",
        lambda host, port: calls.append((host, port)),
    )

    exit_code = main(["serve", "--host", "0.0.0.0", "--port", "9000"])

    assert exit_code == 0
    assert calls == [("0.0.0.0", 9000)]
    assert "http://0.0.0.0:9000/v1/spans" in capsys.readouterr().err


def test_serve_defaults_to_localhost_and_port_4318(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "yolotrace.__main__.run_ingestion_server",
        lambda host, port: calls.append((host, port)),
    )

    main(["serve"])

    assert calls == [("127.0.0.1", 4318)]


def test_serve_stops_cleanly_on_ctrl_c(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(host: str, port: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("yolotrace.__main__.run_ingestion_server", interrupted)

    assert main(["serve"]) == 0
