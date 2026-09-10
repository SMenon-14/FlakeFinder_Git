"""Serialized Modbus service for ONWAY temperature controllers."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
import itertools
import queue
import threading
import time
from typing import Any, Callable, Mapping

PRIORITY_WRITE = 0
PRIORITY_CONNECT = 5
PRIORITY_FAST_POLL = 50
PRIORITY_SLOW_POLL = 100

FAST_REGISTERS = {
    "Temperature": 18504,
    "Power": 2036,
}

SLOW_REGISTERS = {
    "SetTemperature": 18505,
    "P": 18506,
    "I": 18507,
    "D": 18508,
    "Cycle": 18509,
    "Correction": 18550,
    "Filter": 18501,
    "OvertempAlarm": 2490,
}

UNSCALED_AFTER_DIVIDE_REGISTERS = {
    2036, 18523, 2092, 18506, 18507, 18508, 18509, 18501,
}


@dataclass(frozen=True)
class TemperatureSample:
    value: float | None
    monotonic_time: float
    error: str | None = None


@dataclass
class _Request:
    operation: str
    args: tuple[Any, ...]
    future: Future


class PymodbusTemperatureTransport:
    """Own one pymodbus client and reconnect it only from the service thread."""

    def __init__(self, client_factory: Callable[[str], Any]):
        self._client_factory = client_factory
        self._client = None
        self._port = None
        self.connected = False

    def connect(self, port: str) -> bool:
        self.close()
        self._port = str(port)
        client = self._client_factory(self._port)
        try:
            connected = bool(client.connect())
        except Exception:
            try:
                client.close()
            except Exception:
                pass
            raise
        if not connected:
            try:
                client.close()
            except Exception:
                pass
            return False
        self._client = client
        self.connected = True
        return True

    def close(self) -> None:
        client = self._client
        self._client = None
        self.connected = False
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _ensure_connected(self) -> None:
        if self._client is not None and self.connected:
            return
        if not self._port or not self.connect(self._port):
            raise ConnectionError("Temperature controller is not connected.")

    def _reconnect(self) -> None:
        if not self._port or not self.connect(self._port):
            raise ConnectionError("Could not reconnect the temperature controller.")

    def read_register(self, address: int, *, slave: int = 10, retries: int = 2) -> float:
        last_error: Exception | None = None
        for attempt in range(max(1, int(retries))):
            try:
                self._ensure_connected()
                response = self._client.read_holding_registers(
                    int(address), count=1, device_id=int(slave)
                )
                if response.isError():
                    raise IOError(f"Modbus read error at register {address}")
                value = response.registers[0] / 10.0
                if int(address) in UNSCALED_AFTER_DIVIDE_REGISTERS:
                    value *= 10.0
                return float(value)
            except Exception as exc:
                last_error = exc
                if attempt + 1 < max(1, int(retries)):
                    self._reconnect()
                    time.sleep(0.05)
        raise last_error or IOError(f"Could not read register {address}")

    def write_register(self, address: int, value: float, *, slave: int = 10) -> bool:
        return self._write_with_reconnect(
            lambda: self._client.write_register(
                int(address), int(round(float(value) * 10)), device_id=int(slave)
            )
        )

    def write_coil(self, address: int, value: bool, *, slave: int = 10) -> bool:
        return self._write_with_reconnect(
            lambda: self._client.write_coil(
                int(address), bool(value), device_id=int(slave)
            )
        )

    def _write_with_reconnect(self, operation: Callable[[], Any]) -> bool:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                self._ensure_connected()
                response = operation()
                if isinstance(response, bool):
                    return response
                return not response.isError()
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    self._reconnect()
        raise last_error or IOError("Temperature controller write failed")


class TemperatureService:
    """Single-owner temperature polling and write service."""

    def __init__(
        self,
        transport: Any,
        *,
        fast_interval_s: float = 0.25,
        slow_interval_s: float = 5.0,
        slave: int = 10,
        on_update: Callable[[Mapping[str, float]], None] | None = None,
    ):
        self._transport = transport
        self._fast_interval_s = max(0.05, float(fast_interval_s))
        self._slow_interval_s = max(self._fast_interval_s, float(slow_interval_s))
        self._slave = int(slave)
        self._on_update = on_update

        self._queue: queue.PriorityQueue[tuple[int, int, _Request]] = queue.PriorityQueue()
        self._sequence = itertools.count()
        self._lock = threading.RLock()
        self._samples: dict[str, TemperatureSample] = {}
        self._pending_polls: set[str] = set()
        self._connected = False
        self._accepting = True
        self._worker_ident = None
        self._next_fast = float("inf")
        self._next_slow = float("inf")

        self._thread = threading.Thread(
            target=self._worker_main,
            name="temperature-modbus-owner",
            daemon=True,
        )
        self._thread.start()

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def snapshot(self) -> dict[str, TemperatureSample]:
        with self._lock:
            return dict(self._samples)

    def values_snapshot(self) -> dict[str, float | None]:
        with self._lock:
            return {key: sample.value for key, sample in self._samples.items()}

    def connect(self, port: str) -> Future:
        return self._submit("connect", str(port), priority=PRIORITY_CONNECT)

    def set_setpoint(self, value: float) -> Future:
        return self._submit(
            "write_register", 3000, float(value), "SetTemperature",
            priority=PRIORITY_WRITE,
        )

    def set_heating(self, enabled: bool) -> Future:
        return self._submit(
            "write_coil", 10010, bool(enabled), priority=PRIORITY_WRITE
        )

    def shutdown(self, wait: bool = True, timeout: float = 2.0) -> None:
        with self._lock:
            if not self._accepting:
                return
            self._accepting = False
        future: Future = Future()
        self._queue.put((PRIORITY_WRITE, next(self._sequence), _Request("shutdown", (), future)))
        if wait and threading.get_ident() != self._worker_ident:
            self._thread.join(max(0.0, float(timeout)))

    def _submit(self, operation: str, *args: Any, priority: int) -> Future:
        future: Future = Future()
        with self._lock:
            if not self._accepting:
                future.set_exception(RuntimeError("Temperature service is shut down"))
                return future
        self._queue.put((priority, next(self._sequence), _Request(operation, args, future)))
        return future

    def _worker_main(self) -> None:
        self._worker_ident = threading.get_ident()
        while True:
            self._schedule_polls()
            try:
                _priority, _sequence, request = self._queue.get(
                    timeout=self._time_until_poll()
                )
            except queue.Empty:
                continue

            if request.operation == "shutdown":
                try:
                    self._transport.close()
                finally:
                    with self._lock:
                        self._connected = False
                    request.future.set_result(None)
                break
            if not request.future.set_running_or_notify_cancel():
                continue
            try:
                result = self._execute(request.operation, request.args)
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
                request.future.set_exception(RuntimeError("Temperature service is shut down"))

    def _execute(self, operation: str, args: tuple[Any, ...]):
        if operation == "connect":
            connected = bool(self._transport.connect(args[0]))
            if not connected:
                raise ConnectionError(f"Could not connect on {args[0]}")
            now = time.monotonic()
            with self._lock:
                self._connected = True
                self._next_fast = now
                self._next_slow = now
            return True
        if operation == "poll":
            return self._poll_value(str(args[0]), int(args[1]))
        if operation == "write_register":
            address, value, key = args
            success = bool(self._transport.write_register(
                address, value, slave=self._slave
            ))
            if not success:
                raise IOError(f"Write to temperature register {address} failed")
            self._publish_value(str(key), float(value))
            return True
        if operation == "write_coil":
            address, value = args
            success = bool(self._transport.write_coil(
                address, value, slave=self._slave
            ))
            if not success:
                raise IOError(f"Write to temperature coil {address} failed")
            return True
        raise ValueError(f"Unknown temperature operation: {operation}")

    def _poll_value(self, key: str, address: int):
        try:
            value = float(self._transport.read_register(address, slave=self._slave))
        except BaseException as exc:
            with self._lock:
                previous = self._samples.get(key)
                previous_value = previous.value if previous is not None else None
                self._samples[key] = TemperatureSample(
                    previous_value, time.monotonic(), str(exc)
                )
            raise
        else:
            self._publish_value(key, value)
            return value
        finally:
            with self._lock:
                self._pending_polls.discard(key)

    def _publish_value(self, key: str, value: float) -> None:
        with self._lock:
            self._samples[key] = TemperatureSample(
                float(value), time.monotonic(), None
            )
        if self._on_update is not None:
            try:
                self._on_update({key: float(value)})
            except Exception:
                pass

    def _schedule_polls(self) -> None:
        now = time.monotonic()
        scheduled: list[tuple[int, str, int]] = []
        with self._lock:
            if not self._connected:
                return
            if self._next_fast <= now:
                for key, address in FAST_REGISTERS.items():
                    if key not in self._pending_polls:
                        self._pending_polls.add(key)
                        scheduled.append((PRIORITY_FAST_POLL, key, address))
                self._next_fast = now + self._fast_interval_s
            if self._next_slow <= now:
                for key, address in SLOW_REGISTERS.items():
                    if key not in self._pending_polls:
                        self._pending_polls.add(key)
                        scheduled.append((PRIORITY_SLOW_POLL, key, address))
                self._next_slow = now + self._slow_interval_s

        for priority, key, address in scheduled:
            self._submit("poll", key, address, priority=priority)

    def _time_until_poll(self) -> float:
        with self._lock:
            if not self._connected:
                return 0.1
            deadline = min(self._next_fast, self._next_slow)
        return max(0.001, min(0.1, deadline - time.monotonic()))
