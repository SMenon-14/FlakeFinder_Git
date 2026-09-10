import threading
import time
import unittest

from src.microscope.onway_motion_service import MotionService


class FakeController:
    def __init__(self, *, connected=False, position_delay=0.0):
        self.dll_loaded = False
        self.connected = connected
        self.position_delay = position_delay
        self.fail_stop_axis = None
        self.positions = {3: 1.25, 4: -2.5, 5: 12.0}
        self.calls = []
        self.thread_ids = set()
        self.block_started = threading.Event()
        self.block_release = threading.Event()
        self._lock = threading.Lock()

    def _record(self, name, *args):
        with self._lock:
            self.calls.append((name, *args))
            self.thread_ids.add(threading.get_ident())

    def load_dll(self, path):
        self._record("load_dll", path)
        self.dll_loaded = True
        return 0

    def connect(self, port, logger=lambda _message: None):
        self._record("connect", port)
        self.connected = True
        logger(f"connected {port}")
        return 0

    def disconnect(self, logger=lambda _message: None):
        self._record("disconnect")
        self.connected = False
        logger("disconnected")
        return 0

    def send_param(self, axis, parameter, value):
        self._record("send_param", axis, parameter, value)
        return 0

    def move_abs(self, axis, position, velocity=None, acceleration=None):
        self._record("move_abs", axis, position)
        self.positions[axis] = position
        return 0

    def get_pos(self, axis):
        self._record("get_pos", axis)
        if self.position_delay:
            time.sleep(self.position_delay)
        return 0, self.positions[axis]

    def at_speed_timed(self, axis, velocity, acceleration, duration_s):
        self._record("jog", axis, velocity)
        return 0

    def stop(self, axis):
        self._record("stop", axis)
        if axis == self.fail_stop_axis:
            raise RuntimeError("simulated stop failure")
        return 0

    def block(self):
        self._record("block")
        self.block_started.set()
        if not self.block_release.wait(1.0):
            raise TimeoutError("test did not release blocked fake call")


class MotionServiceTests(unittest.TestCase):
    def test_all_controller_calls_use_one_owner_thread(self):
        fake = FakeController()
        service = MotionService(fake)
        try:
            futures = [
                service.submit("send_param", index, 1, 0.5)
                for index in range(12)
            ]
            for future in futures:
                self.assertEqual(future.result(timeout=1.0), 0)

            self.assertEqual(len(fake.thread_ids), 1)
            self.assertNotIn(threading.get_ident(), fake.thread_ids)
        finally:
            service.shutdown()

    def test_priority_stop_overtakes_queued_normal_moves(self):
        fake = FakeController()
        service = MotionService(fake)
        try:
            blocked = service.submit("block")
            self.assertTrue(fake.block_started.wait(1.0))
            move_one = service.submit("move_abs", 3, 10.0)
            move_two = service.submit("move_abs", 4, 20.0)
            stop = service.stop_jog(3)
            fake.block_release.set()

            blocked.result(timeout=1.0)
            stop.result(timeout=1.0)
            move_one.result(timeout=1.0)
            move_two.result(timeout=1.0)

            names = [call[0] for call in fake.calls]
            self.assertEqual(names[:4], ["block", "stop", "move_abs", "move_abs"])
        finally:
            fake.block_release.set()
            service.shutdown()

    def test_position_polling_publishes_nonblocking_snapshot(self):
        fake = FakeController(connected=True, position_delay=0.04)
        service = MotionService(fake, poll_axes={"A": 3, "B": 4, "C": 5},
                                poll_interval_s=0.1)
        try:
            deadline = time.monotonic() + 1.0
            snapshot = {}
            while time.monotonic() < deadline:
                snapshot = service.position_snapshot()
                if set(snapshot) == {3, 4, 5}:
                    break
                time.sleep(0.01)

            self.assertEqual(set(snapshot), {3, 4, 5})
            self.assertEqual(snapshot[3].value, 1.25)
            self.assertEqual(snapshot[4].value, -2.5)
            self.assertEqual(snapshot[5].value, 12.0)

            started = time.monotonic()
            service.position_snapshot()
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.01)
        finally:
            service.shutdown()

    def test_jog_is_renewed_and_stop_cancels_future_renewals(self):
        fake = FakeController(connected=True)
        service = MotionService(fake)
        try:
            service.start_jog(2, 0.5, 1.0, 0.01).result(timeout=1.0)
            time.sleep(0.07)
            service.stop_jog(2).result(timeout=1.0)
            count_after_stop = sum(call[0] == "jog" for call in fake.calls)
            time.sleep(0.06)

            final_count = sum(call[0] == "jog" for call in fake.calls)
            self.assertGreaterEqual(count_after_stop, 2)
            self.assertEqual(final_count, count_after_stop)
            self.assertIn(("stop", 2), fake.calls)
        finally:
            service.shutdown()

    def test_disconnect_runs_after_an_individual_stop_failure(self):
        fake = FakeController(connected=True)
        fake.fail_stop_axis = 1
        service = MotionService(fake)
        try:
            _result, stop_errors = service.stop_and_disconnect(
                [0, 1, 2]
            ).result(timeout=1.0)

            self.assertFalse(service.connected)
            self.assertEqual(stop_errors, ((1, "simulated stop failure"),))
            self.assertEqual(
                [call[0] for call in fake.calls],
                ["stop", "stop", "stop", "disconnect"],
            )
        finally:
            service.shutdown()


if __name__ == "__main__":
    unittest.main()
