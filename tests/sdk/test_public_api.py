import tomllib
from pathlib import Path

import yolotrace
from yolotrace import sdk

STABLE_API = [
    "ExportError",
    "InstrumentationPolicy",
    "PolicyError",
    "Run",
    "Span",
    "SpanContext",
    "SpanEvent",
    "SpanKind",
    "__version__",
    "apply_policy",
    "configure",
    "continue_trace",
    "current_span",
    "export_spans",
    "extract",
    "finished_spans",
    "inject",
    "instrument_anthropic",
    "instrument_langchain",
    "instrument_openai",
    "instrument_openai_agents",
    "load_policy",
    "propagate_context",
    "running_spans",
    "runs",
    "start_span",
    "trace",
]


def test_public_api_is_exactly_the_stable_surface() -> None:
    assert sorted(yolotrace.__all__) == STABLE_API
    assert sorted(sdk.__all__) == STABLE_API


def test_version_matches_project_metadata() -> None:
    pyproject = Path(__file__).parents[2] / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    assert yolotrace.__version__ == project["version"] == sdk.__version__
