import tomllib
from pathlib import Path

import yolotrace
from yolotrace import sdk


def test_public_api_is_exactly_the_stable_surface() -> None:
    assert yolotrace.__all__ == ["__version__"]
    assert sdk.__all__ == ["__version__"]


def test_version_matches_project_metadata() -> None:
    pyproject = Path(__file__).parents[2] / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    assert yolotrace.__version__ == project["version"] == sdk.__version__
