import pytest

from yolotrace.sdk import tracing


@pytest.fixture(autouse=True)
def clear_spans() -> None:
    tracing._finished.clear()
