"""Single-owner execution service for the MCC motion controller.

The MCC DLL is not called from Tk callbacks or from competing scan threads.
Every hardware operation is executed by one worker, while position polling is
published as a cheap, thread-safe snapshot for the UI.
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
import itertools
import queue
import threading
import time
from typing import Any, Iterable, Mapping, Sequence


PRIORITY_STOP = 0
PRIORITY_JOG = 10
PRIORITY_INTERACTIVE = 30
PRIORITY_NORMAL = 50
PRIORITY_POLL = 100


@dataclass(frozen=True)
class PositionSample:
    """Most recent position result for one controller axis."""

    value: float
    return_code: Any
    monotonic_time: float
    error: str | None = None


@dataclass
class _Request:
    operation: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    future: Future


@dataclass
class _JogState:
    velocity: float
    acceleration: float
    duration_s: float
    generation: int
    next_due: float
    pending: bool = False


class MotionService:
    """Serialize MCC access and provide non-blocking UI-friendly operations.

    The regular controller methods remain synchronous so existing scan worker
    code keeps its sequencing guarantees. Tk callbacks should use ``submit``,
    ``submit_batch``, ``start_jog``, or ``stop_jog`` instead.
    """

    def __init__(
        self,
        controller: Any,
        poll_axes: Mapping[str, int] | Iterable[int] = (),
        poll_interval_s: float = 0.5,
    ):
        self._controller = controller
        if isinstance(poll_axes, Mapping):
            self._poll_axes = tuple(dict.fromkeys(int(v) for v in poll_axes.values()))
        else:
            self._poll_axes = tuple(dict.fromkeys(int(v) for v in poll_axes))
        self._poll_interval_s = max(0.05, float(poll_interval_s))

        self._queue: queue.PriorityQueue[tuple[int, int, _Request]] = queue.PriorityQueue()
        self._sequence = itertools.count()
        self._state_lock = threading.RLock()
        self._positions: dict[int, PositionSample] = {}
        self._jogs: dict[int, _JogState] = {}
        self._jog_generation = itertools.count(1)
        self._poll_pending: set[int] = set()
        self._dll_loaded = bool(getattr(controller, "dll_loaded", False))
        self._connected = bool(getattr(controller, "connected", False))
        self._accepting = True
        self._worker_ident: int | None = None
        self._next_poll = time.monotonic()

        self._thread = threading.Thread(
            target=self._worker_main,
            name="MCC-motion-owner",
            daemon=True,
        )
        self._thread.start()

    @property
    def dll_loaded(self) -> bool:
        with self._state_lock:
            return self._dll_loaded

    @property
    def connected(self) -> bool:
        with self._state_lock:
            return self._connected

    def position_snapshot(self) -> dict[int, PositionSample]:
        """Return cached positions without touching the hardware."""
        with self._state_lock:
            return dict(self._positions)

    def submit(
        self,
        operation: str,
        *args: Any,
        priority: int = PRIORITY_NORMAL,
        **kwargs: Any,
    ) -> Future:
        future: Future = Future()
        with self._state_lock:
            accepting = self._accepting
        if not accepting:
            future.set_exception(RuntimeError("Motion service is shut down"))
            return future
        request = _Request(operation, tuple(args), dict(kwargs), future)
        self._queue.put((int(priority), next(self._sequence), request))
        return future

    def submit_batch(
        self,
        calls: Sequence[tuple[str, tuple[Any, ...], dict[str, Any]]],
        *,
        priority: int = PRIORITY_NORMAL,
    ) -> Future:
        """Run a group of calls consecutively on the controller owner thread."""
        normalized = tuple(
            (str(name), tuple(args), dict(kwargs))
            for name, args, kwargs in calls
        )
        return self.submit("__batch__", normalized, priority=priority)

    def start_jog(
        self,
        axis: int,
        velocity: float,
        acceleration: float,
        duration_s: float,
    ) -> Future:
        """Start or update a renewed timed jog without Tk timer callbacks."""
        axis = int(axis)
        generation = next(self._jog_generation)
        state = _JogState(
            float(velocity),
            float(acceleration),
            max(0.001, float(duration_s)),
            generation,
            time.monotonic(),
            pending=True,
        )
        with self._state_lock:
            if not self._accepting:
                future: Future = Future()
                future.set_exception(RuntimeError("Motion service is shut down"))
                return future
            self._jogs[axis] = state
        return self.submit("__jog__", axis, generation, priority=PRIORITY_JOG)

    def stop_jog(self, axis: int) -> Future:
        """Cancel jog renewals first, then queue a priority hardware stop."""
        axis = int(axis)
        with self._state_lock:
            self._jogs.pop(axis, None)
        return self.submit("stop", axis, priority=PRIORITY_STOP)

    def stop_all(self, axes: Iterable[int]) -> Future:
        axes = tuple(dict.fromkeys(int(axis) for axis in axes))
        with self._state_lock:
            for axis in axes:
                self._jogs.pop(axis, None)
        return self.submit("__stop_all__", axes, priority=PRIORITY_STOP)

    def stop_and_disconnect(
        self,
        axes: Iterable[int],
        logger=lambda _message: None,
    ) -> Future:
        """Attempt every safety stop, then disconnect even if a stop fails."""
        axes = tuple(dict.fromkeys(int(axis) for axis in axes))
        with self._state_lock:
            self._jogs.clear()
        return self.submit(
            "__stop_and_disconnect__", axes, logger, priority=PRIORITY_STOP
        )

    # Synchronous facade used by scan/background code.
    def load_dll(self, dll_path: str):
        return self._call("load_dll", dll_path)

    def connect(self, com_port: str, logger=lambda _message: None):
        return self._call("connect", com_port, logger=logger)

    def disconnect(self, logger=lambda _message: None):
        with self._state_lock:
            self._jogs.clear()
        return self._call("disconnect", logger=logger, priority=PRIORITY_STOP)

    def send_param(self, axis: int, param_index: int, value: float):
        return self._call("send_param", axis, param_index, value)

    def at_speed(self, axis: int, velocity: float, acceleration: float):
        return self._call("at_speed", axis, velocity, acceleration, priority=PRIORITY_JOG)

    def at_speed_timed(
        self,
        axis: int,
        velocity: float,
        acceleration: float,
        duration_s: float,
    ):
        return self._call(
            "at_speed_timed",
            axis,
            velocity,
            acceleration,
            duration_s,
            priority=PRIORITY_JOG,
        )

    def stop(self, axis: int):
        with self._state_lock:
            self._jogs.pop(int(axis), None)
        return self._call("stop", axis, priority=PRIORITY_STOP)

    def move_abs(self, axis: int, position: float, v: float = None, a: float = None):
        return self._call("move_abs", axis, position, v, a)

    def get_pos(self, axis: int):
        return self._call("get_pos", axis)

    def read_param(self, axis: int, param_index: int):
        return self._call("read_param", axis, param_index)

    def shutdown(self, wait: bool = True, timeout: float = 2.0) -> None:
        with self._state_lock:
            if not self._accepting:
                return
            self._accepting = False
            self._jogs.clear()
        sentinel = Future()
        request = _Request("__shutdown__", (), {}, sentinel)
        self._queue.put((PRIORITY_STOP, next(self._sequence), request))
        if wait and threading.get_ident() != self._worker_ident:
            self._thread.join(max(0.0, float(timeout)))

    def _call(self, operation: str, *args: Any, priority: int = PRIORITY_NORMAL, **kwargs: Any):
        if threading.get_ident() == self._worker_ident:
            return self._execute(operation, args, kwargs)
        return self.submit(operation, *args, priority=priority, **kwargs).result()

    def _worker_main(self) -> None:
        self._worker_ident = threading.get_ident()
        while True:
            self._schedule_due_work()
            timeout = self._time_until_due_work()
            try:
                _priority, _sequence, request = self._queue.get(timeout=timeout)
            except queue.Empty:
                continue

            if request.operation == "__shutdown__":
                request.future.set_result(None)
                break
            if not request.future.set_running_or_notify_cancel():
                continue
            try:
                result = self._execute(request.operation, request.args, request.kwargs)
            except BaseException as exc:
                request.future.set_exception(exc)
            else:
                request.future.set_result(result)

        while True:
            try:
                _priority, _sequence, request = self._queue.get_nowait()
            except queue.Empty:
                break
            if not request.future.done():
                request.future.set_exception(RuntimeError("Motion service is shut down"))

    def _execute(self, operation: str, args: tuple[Any, ...], kwargs: dict[str, Any]):
        if operation == "__batch__":
            return tuple(self._execute(name, call_args, call_kwargs)
                         for name, call_args, call_kwargs in args[0])
        if operation == "__poll__":
            return self._poll_axis(int(args[0]))
        if operation == "__jog__":
            return self._jog_axis(int(args[0]), int(args[1]))
        if operation == "__stop_and_disconnect__":
            axes, logger = args
            stop_errors = self._execute("__stop_all__", (axes,), {})
            disconnect_result = self._execute(
                "disconnect", (), {"logger": logger}
            )
            return disconnect_result, stop_errors
        if operation == "__stop_all__":
            stop_errors = []
            for axis in args[0]:
                try:
                    self._execute("stop", (axis,), {})
                except BaseException as exc:
                    stop_errors.append((axis, str(exc)))
            return tuple(stop_errors)

        method = getattr(self._controller, operation)
        result = method(*args, **kwargs)
        self._record_result(operation, args, result)
        return result

    def _record_result(self, operation: str, args: tuple[Any, ...], result: Any) -> None:
        with self._state_lock:
            if operation == "load_dll":
                self._dll_loaded = bool(getattr(self._controller, "dll_loaded", True))
            elif operation == "connect":
                self._connected = bool(getattr(self._controller, "connected", True))
                self._next_poll = time.monotonic()
            elif operation == "disconnect":
                self._connected = False
                self._positions.clear()
                self._poll_pending.clear()
                self._jogs.clear()
            elif operation == "stop" and args:
                self._jogs.pop(int(args[0]), None)
            elif operation == "get_pos" and args:
                return_code, position = result
                self._positions[int(args[0])] = PositionSample(
                    float(position), return_code, time.monotonic()
                )

    def _poll_axis(self, axis: int):
        try:
            result = self._controller.get_pos(axis)
            self._record_result("get_pos", (axis,), result)
            return result
        except BaseException as exc:
            with self._state_lock:
                previous = self._positions.get(axis)
                value = previous.value if previous is not None else float("nan")
                return_code = previous.return_code if previous is not None else None
                self._positions[axis] = PositionSample(
                    value, return_code, time.monotonic(), str(exc)
                )
            raise
        finally:
            with self._state_lock:
                self._poll_pending.discard(axis)

    def _jog_axis(self, axis: int, generation: int):
        with self._state_lock:
            state = self._jogs.get(axis)
            if state is None or state.generation != generation:
                return None
            velocity = state.velocity
            acceleration = state.acceleration
            duration_s = state.duration_s
        try:
            result = self._controller.at_speed_timed(
                axis, velocity, acceleration, duration_s
            )
        except BaseException:
            with self._state_lock:
                state = self._jogs.get(axis)
                if state is not None and state.generation == generation:
                    self._jogs.pop(axis, None)
            raise
        with self._state_lock:
            state = self._jogs.get(axis)
            if state is not None and state.generation == generation:
                state.pending = False
                renewal_s = max(0.015, min(0.05, duration_s * 0.8))
                state.next_due = time.monotonic() + renewal_s
        return result

    def _schedule_due_work(self) -> None:
        now = time.monotonic()
        jog_requests: list[tuple[int, int]] = []
        poll_axes: tuple[int, ...] = ()
        with self._state_lock:
            for axis, state in self._jogs.items():
                if not state.pending and state.next_due <= now:
                    state.pending = True
                    jog_requests.append((axis, state.generation))

            if self._connected and self._poll_axes and self._next_poll <= now:
                poll_axes = tuple(axis for axis in self._poll_axes
                                  if axis not in self._poll_pending)
                self._poll_pending.update(poll_axes)
                self._next_poll = now + self._poll_interval_s

        for axis, generation in jog_requests:
            self.submit("__jog__", axis, generation, priority=PRIORITY_JOG)
        for axis in poll_axes:
            self.submit("__poll__", axis, priority=PRIORITY_POLL)

    def _time_until_due_work(self) -> float:
        now = time.monotonic()
        deadlines: list[float] = []
        with self._state_lock:
            if self._connected and self._poll_axes:
                deadlines.append(self._next_poll)
            deadlines.extend(
                state.next_due for state in self._jogs.values() if not state.pending
            )
        if not deadlines:
            return 0.1
        return max(0.001, min(0.1, min(deadlines) - now))
