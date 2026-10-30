"""In-memory storage for the spans the ingestion API has accepted."""

from __future__ import annotations

import threading
from collections.abc import Sequence

from yolotrace.sdk.tracing import Span


class TraceStore:
    """Hold accepted spans in process memory. Requests are handled on several threads, so access is locked.

    There is one record per trace and span ID. A span that is still running is replaced by
    the next copy that arrives, so a later snapshot or the finished span wins. A finished span
    is never replaced, so a late copy that still says "running" cannot undo it, and a retried
    finished span is stored once.

    The spans are lost when the process stops. Persistent storage is a separate feature.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spans: dict[tuple[str, str], Span] = {}

    def add(self, spans: Sequence[Span]) -> None:
        """Store ``spans``, replacing a running copy of the same span and keeping a finished one as it is.

        Spans may arrive in any order. A child may be stored before its parent, and a
        run may be stored before all of its spans have ended.
        """
        with self._lock:
            for span in spans:
                key = (span.trace_id, span.span_id)
                stored = self._spans.get(key)
                if stored is None or stored.end_time is None:
                    self._spans[key] = span

    def spans(self) -> list[Span]:
        """Return a copy of every stored span, in the order each was first received."""
        with self._lock:
            return list(self._spans.values())
