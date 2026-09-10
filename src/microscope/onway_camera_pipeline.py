"""Small background workers used by the microscope camera preview."""

from __future__ import annotations

import threading
from typing import Any, Callable


class LatestItemWorker:
    """Process one item at a time while retaining only the newest pending item.

    If processing is slower than the producer, intermediate submissions are
    deliberately replaced instead of building a latency-producing frame queue.
    """

    def __init__(self, processor: Callable[[Any], Any], *, name: str):
        self._processor = processor
        self._condition = threading.Condition()
        self._pending: tuple[int, Any] | None = None
        self._latest_result: tuple[int, Any, Exception | None] | None = None
        self._stopping = False
        self._dropped = 0
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    @property
    def dropped_count(self) -> int:
        with self._condition:
            return self._dropped

    def submit(self, sequence: int, payload: Any) -> bool:
        """Publish work, replacing an older item that has not started yet."""
        with self._condition:
            if self._stopping:
                return False
            if self._pending is not None:
                self._dropped += 1
            self._pending = (int(sequence), payload)
            self._condition.notify()
            return True

    def latest_result_after(
        self, sequence: int
    ) -> tuple[int, Any, Exception | None] | None:
        with self._condition:
            result = self._latest_result
            if result is None or result[0] <= sequence:
                return None
            return result

    def shutdown(self, timeout: float = 2.0) -> None:
        with self._condition:
            self._stopping = True
            self._pending = None
            self._condition.notify_all()
        if threading.get_ident() != self._thread.ident:
            self._thread.join(max(0.0, float(timeout)))

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                sequence, payload = self._pending
                self._pending = None

            try:
                result = self._processor(payload)
            except Exception as exc:
                completed = (sequence, None, exc)
            else:
                completed = (sequence, result, None)

            with self._condition:
                if self._latest_result is None or sequence >= self._latest_result[0]:
                    self._latest_result = completed
