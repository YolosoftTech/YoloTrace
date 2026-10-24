import asyncio
import inspect

import pytest

from yolotrace.sdk import tracing


@pytest.fixture(autouse=True)
def clear_spans() -> None:
    tracing._finished.clear()
    tracing._running.clear()


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    """Run ``async def`` tests to completion on a fresh event loop; they take no fixtures."""
    if inspect.iscoroutinefunction(pyfuncitem.obj):
        asyncio.run(pyfuncitem.obj())
        return True
    return None
