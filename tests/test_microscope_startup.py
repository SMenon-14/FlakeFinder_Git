import threading
import unittest
from unittest import mock

from src.microscope.onway_microscope_panel import MicroscopeControlPanel


class ValueStub:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class StartupElevatorStub:
    def __init__(self):
        self.is_connected = True
        self.current_position = None
        self.move_up_calls = []
        self.stop_calls = 0

    def move_up(self, speed):
        self.move_up_calls.append(speed)
        return True

    def stop(self):
        self.stop_calls += 1
        return True


class StartupNosepieceStub:
    def __init__(self, send_ok=True):
        self.is_connected = True
        self.current_lens = None
        self.send_ok = send_ok
        self.commands = []
        self.waits = []

    def send_lens_command(self, lens):
        self.commands.append(lens)
        return self.send_ok

    def wait_for_lens(self, lens, timeout_s, cancel_event):
        self.waits.append((lens, timeout_s, cancel_event))
        return False


class ImmediateThread:
    def __init__(self, target, daemon=True):
        self.target = target

    def start(self):
        self.target()


class DeferredThread:
    created = []

    def __init__(self, target, daemon=True):
        self.target = target
        self.started = False
        self.__class__.created.append(self)

    def start(self):
        self.started = True

    def is_alive(self):
        return self.started


class MicroscopeStartupTests(unittest.TestCase):
    def test_untracked_lift_does_not_require_a_position_read(self):
        panel = object.__new__(MicroscopeControlPanel)
        panel.elevator = StartupElevatorStub()
        panel._set_focus_position_var = lambda _position: None

        moved = panel._run_untracked_focus_move_distance("up", 0.001, speed=32)

        self.assertTrue(moved)
        self.assertEqual(len(panel.elevator.move_up_calls), 1)
        self.assertEqual(panel.elevator.stop_calls, 1)
        self.assertIsNone(panel.elevator.current_position)

    def _startup_panel(self, send_ok=True):
        panel = object.__new__(MicroscopeControlPanel)
        panel.elevator = StartupElevatorStub()
        panel.nosepiece = StartupNosepieceStub(send_ok=send_ok)
        panel.move_stop_event = threading.Event()
        panel.busy_lock = threading.Lock()
        panel.objective_transaction_lock = threading.Lock()
        panel._startup_objective_lock = threading.Lock()
        panel._startup_objective_active = False
        panel.objective_change_timeout_var = ValueStub(12.0)
        panel.move_thread = None
        panel._prepare_new_motion = lambda: True
        panel._clear_move_thread_if_current = lambda: None

        return panel

    def test_startup_lifts_selects_10x_and_returns_same_distance(self):
        panel = self._startup_panel()

        moves = []
        displays = []
        logs = []
        panel._run_untracked_focus_move_distance = (
            lambda direction, distance, speed, stop_event=None:
            moves.append((direction, distance, speed, stop_event)) or True
        )
        panel._set_current_lens_var = displays.append
        panel.log = logs.append

        with mock.patch("src.microscope.onway_microscope_panel.threading.Thread", ImmediateThread):
            panel.initialize_default_objective()

        self.assertEqual(len(moves), 2)
        self.assertEqual(moves[0][:3], ("up", 5.0, panel.OBJECTIVE_ELEVATOR_SPEED))
        self.assertEqual(moves[1][:3], ("down", 5.0, panel.OBJECTIVE_ELEVATOR_SPEED))
        self.assertEqual(panel.nosepiece.commands, [panel.DEFAULT_OBJECTIVE_LENS])
        self.assertEqual(panel.nosepiece.current_lens, panel.DEFAULT_OBJECTIVE_LENS)
        self.assertEqual(displays[-1], panel.DEFAULT_OBJECTIVE_LENS)
        self.assertTrue(any("moving focus down" in entry.lower() for entry in logs))

    def test_startup_returns_down_after_objective_command_failure(self):
        panel = self._startup_panel(send_ok=False)
        moves = []
        panel._run_untracked_focus_move_distance = (
            lambda direction, distance, speed, stop_event=None:
            moves.append((direction, distance, speed, stop_event)) or True
        )
        panel._set_current_lens_var = lambda _lens: None
        panel.log = lambda _message: None

        with mock.patch("src.microscope.onway_microscope_panel.threading.Thread", ImmediateThread):
            panel.initialize_default_objective()

        self.assertEqual([move[0] for move in moves], ["up", "down"])
        self.assertIsNone(panel.nosepiece.current_lens)

    def test_duplicate_startup_request_does_not_cancel_active_sequence(self):
        panel = self._startup_panel()
        prepare_calls = []
        logs = []
        panel._prepare_new_motion = lambda: prepare_calls.append(True) or True
        panel._set_current_lens_var = lambda _lens: None
        panel.log = logs.append
        DeferredThread.created.clear()

        with mock.patch("src.microscope.onway_microscope_panel.threading.Thread", DeferredThread):
            panel.initialize_default_objective()
            panel.initialize_default_objective()

        self.assertEqual(len(prepare_calls), 1)
        self.assertEqual(len(DeferredThread.created), 1)
        self.assertTrue(any("duplicate request ignored" in entry for entry in logs))


if __name__ == "__main__":
    unittest.main()
