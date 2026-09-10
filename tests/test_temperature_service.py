import collections
import threading
import time
import unittest

from src.microscope.onway_temperature_service import (
    FAST_REGISTERS,
    SLOW_REGISTERS,
    TemperatureService,
)


class FakeTemperatureTransport:
    def __init__(self, write_delay=0.0):
        self.connected = False
        self.calls = []
        self.thread_ids = set()
        self.read_counts = collections.Counter()
        self.write_delay = write_delay
        self.active_writes = 0
        self.max_active_writes = 0
        self._lock = threading.Lock()

    def _record(self, name, *args):
        with self._lock:
            self.calls.append((name, *args))
            self.thread_ids.add(threading.get_ident())

    def connect(self, port):
        self._record("connect", port)
        self.connected = True
        return True

    def close(self):
        self._record("close")
        self.connected = False

    def read_register(self, address, *, slave=10):
        self._record("read", address, slave)
        with self._lock:
            self.read_counts[address] += 1
        return float(address) / 10.0

    def write_register(self, address, value, *, slave=10):
        self._record("write_register", address, value, slave)
        with self._lock:
            self.active_writes += 1
            self.max_active_writes = max(self.max_active_writes, self.active_writes)
        try:
            if self.write_delay:
                time.sleep(self.write_delay)
        finally:
            with self._lock:
                self.active_writes -= 1
        return True

    def write_coil(self, address, value, *, slave=10):
        self._record("write_coil", address, value, slave)
        with self._lock:
            self.active_writes += 1
            self.max_active_writes = max(self.max_active_writes, self.active_writes)
        try:
            if self.write_delay:
                time.sleep(self.write_delay)
        finally:
            with self._lock:
                self.active_writes -= 1
        return True


class TemperatureServiceTests(unittest.TestCase):
    def test_fast_registers_are_polled_more_often_than_slow_registers(self):
        transport = FakeTemperatureTransport()
        service = TemperatureService(
            transport,
            fast_interval_s=0.03,
            slow_interval_s=0.15,
        )
        try:
            service.connect("COM_TEST").result(timeout=1.0)
            time.sleep(0.23)

            fast_count = transport.read_counts[FAST_REGISTERS["Temperature"]]
            slow_count = transport.read_counts[SLOW_REGISTERS["P"]]
            self.assertGreaterEqual(fast_count, 4)
            self.assertGreaterEqual(slow_count, 1)
            self.assertGreater(fast_count, slow_count)
        finally:
            service.shutdown()

    def test_concurrent_temperature_writes_are_serialized_on_one_owner(self):
        transport = FakeTemperatureTransport(write_delay=0.01)
        service = TemperatureService(transport, fast_interval_s=1, slow_interval_s=5)
        try:
            service.connect("COM_TEST").result(timeout=1.0)
            futures = []
            submit_lock = threading.Lock()

            def submit_setpoint(value):
                future = service.set_setpoint(value)
                with submit_lock:
                    futures.append(future)

            callers = [
                threading.Thread(target=submit_setpoint, args=(value,))
                for value in range(8)
            ]
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join()
            futures.append(service.set_heating(True))

            for future in futures:
                self.assertTrue(future.result(timeout=2.0))

            self.assertEqual(transport.max_active_writes, 1)
            self.assertEqual(len(transport.thread_ids), 1)
            self.assertNotIn(threading.get_ident(), transport.thread_ids)
        finally:
            service.shutdown()

    def test_polling_and_successful_write_publish_snapshots(self):
        transport = FakeTemperatureTransport()
        updates = []
        service = TemperatureService(
            transport,
            fast_interval_s=0.05,
            slow_interval_s=0.2,
            on_update=lambda changes: updates.append(dict(changes)),
        )
        try:
            service.connect("COM_TEST").result(timeout=1.0)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if "Temperature" in service.snapshot():
                    break
                time.sleep(0.01)

            service.set_setpoint(42.5).result(timeout=1.0)
            snapshot = service.snapshot()
            self.assertEqual(snapshot["Temperature"].value, 1850.4)
            self.assertEqual(snapshot["SetTemperature"].value, 42.5)
            self.assertTrue(any(update.get("SetTemperature") == 42.5 for update in updates))
        finally:
            service.shutdown()


if __name__ == "__main__":
    unittest.main()
