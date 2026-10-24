import tomllib
from pathlib import Path

import yolotrace
from yolotrace import sdk

STABLE_API = [
    "Span",
    "SpanKind",
    "__version__",
    "current_span",
    "finished_spans",
    "instrument_anthropic",
    "instrument_openai",
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
