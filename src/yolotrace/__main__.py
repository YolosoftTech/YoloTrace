"""Command line entry point: ``python -m yolotrace run <script> [args...]``.

The ``run`` command instruments every installed LLM library, runs the script unchanged, and
prints a summary of the recorded spans to stderr when the script finishes.
"""

from __future__ import annotations

import argparse
import runpy
import sys
from collections.abc import Sequence

from yolotrace.sdk.tracing import finished_spans
from yolotrace.sdk.zero_code import instrument_installed


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` and run the requested command. Return the process exit code."""
    parser = argparse.ArgumentParser(prog="python -m yolotrace")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "run", help="run a Python script with installed LLM libraries instrumented"
    )
    run.add_argument("script", help="path to the Python script to run")
    run.add_argument("args", nargs=argparse.REMAINDER, help="arguments for the script")
    options = parser.parse_args(argv)

    instrument_installed()
    sys.argv = [options.script, *options.args]
    try:
        runpy.run_path(options.script, run_name="__main__")
    finally:
        _print_summary()
    return 0


def _print_summary() -> None:
    """Write how many spans and LLM calls were recorded, and how many failed, to stderr."""
    spans = finished_spans()
    llm_calls = [span for span in spans if span.kind == "llm"]
    failed = [span for span in spans if span.status == "error"]
    print(
        f"YoloTrace: recorded {len(spans)} spans, {len(llm_calls)} LLM calls, "
        f"{len(failed)} failed",
        file=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
