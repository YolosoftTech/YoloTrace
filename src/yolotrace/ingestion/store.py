"""In-memory storage for the spans the ingestion API has accepted."""

from __future__ import annotations

import threading
from collections.abc import Sequence

from yolotrace.sdk.tracing import Span


class TraceStore:
    """Hold accepted spans in process memory. Requests are handled on several threads, so access is locked.

    The spans are lost when the process stops. Persistent storage is a separate feature.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spans: list[Span] = []

    def add(self, spans: Sequence[Span]) -> None:
        """Store ``spans`` after the spans already held, in the order given."""
        with self._lock:
            self._spans.extend(spans)

    def spans(self) -> list[Span]:
        """Return a copy of every stored span, in the order they were added."""
        with self._lock:
            return list(self._spans)
