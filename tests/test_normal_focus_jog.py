import threading
import unittest
from unittest import mock

from src.microscope.onway_microscope_panel import MicroscopeControlPanel


class ValueStub:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class ImmediateThread:
    def __init__(self, target, daemon=True):
        self.target = target

    def start(self):
        self.target()

    def is_alive(self):
        return False


class ImmediateTimer:
    def __init__(self, _interval, function, args=()):
        self.function = function
        self.args = args
        self.daemon = False
        self.cancelled = False

    def start(self):
        if not self.cancelled:
            self.function(*self.args)

    def cancel(self):
        self.cancelled = True


class NormalJogElevatorStub:
    def __init__(self, stop_event):
        self.is_connected = True
        self.current_position = 12.345
        self.last_position_error = None
        self.stop_event = stop_event
        self.up_commands = []
        self.stop_calls = 0
        self.position_reads = 0
        self.motion_queries = 0

    def get_current_position(self):
        self.position_reads += 1
        return self.current_position

    def _query_current_position(self, clear_input=True):
        self.motion_queries += 1
        self.current_position = 12.300
        self.stop_event.set()
        return self.current_position

    def move_up(self, speed):
        self.up_commands.append(speed)
        self.stop_event.set()
        return True

    def move_down(self, speed):
        raise AssertionError("The up jog must not issue down commands.")

    def stop(self):
        self.stop_calls += 1
        return True


class NormalFocusJogTests(unittest.TestCase):
    def test_normal_jog_uses_encoder_for_safety_and_final_display(self):
        panel = object.__new__(MicroscopeControlPanel)
        panel.move_stop_event = threading.Event()
        panel.elevator = NormalJogElevatorStub(panel.move_stop_event)
        panel.speed_var = ValueStub(32)
        panel.active_hold_direction = None
        panel.move_thread = None
        panel._require_elevator = lambda: True
        panel._get_focus_lower_limit = lambda: 60.0
        panel._prepare_new_motion = lambda: True
        panel._clear_move_thread_if_current = lambda: None
        delayed_reads = []
        panel._schedule_normal_jog_position_read = lambda: delayed_reads.append(True)
        displayed_positions = []
        panel._set_focus_position_var = displayed_positions.append
        panel.log = lambda _message: None

        with (
            mock.patch("src.microscope.onway_microscope_panel.threading.Thread", ImmediateThread),
            mock.patch("src.microscope.onway_microscope_panel.time.sleep"),
        ):
            panel.start_hold_move("up")

        self.assertEqual(panel.elevator.up_commands, [32])
        self.assertEqual(panel.elevator.motion_queries, 0)
        self.assertEqual(panel.elevator.position_reads, 0)
        self.assertEqual(panel.elevator.stop_calls, 1)
        self.assertEqual(displayed_positions, [])
        self.assertEqual(delayed_reads, [True])
        self.assertAlmostEqual(panel.elevator.current_position, 12.329)

    def test_delayed_read_updates_the_display_from_the_encoder(self):
        panel = object.__new__(MicroscopeControlPanel)
        panel._normal_jog_read_lock = threading.Lock()
        panel._normal_jog_read_timer = None
        panel._normal_jog_read_generation = 0
        panel._normal_jog_active = False
        panel._closing = False
        panel.move_thread = None
        panel.elevator = NormalJogElevatorStub(threading.Event())
        panel.elevator.current_position = 12.300
        displayed_positions = []
        panel._set_focus_position_var = displayed_positions.append
        panel.log = lambda _message: None

        with mock.patch("src.microscope.onway_microscope_panel.threading.Timer", ImmediateTimer):
            panel._schedule_normal_jog_position_read()

        self.assertEqual(panel.elevator.position_reads, 1)
        self.assertEqual(displayed_positions, [12.300])

    def test_normal_jog_schedules_delayed_read_after_an_external_stop(self):
        panel = object.__new__(MicroscopeControlPanel)
        panel.move_stop_event = threading.Event()
        panel.elevator = NormalJogElevatorStub(panel.move_stop_event)
        panel.speed_var = ValueStub(32)
        panel.active_hold_direction = None
        panel.move_thread = None
        panel._normal_jog_active = False
        panel._require_elevator = lambda: True
        panel._get_focus_lower_limit = lambda: 60.0
        panel._prepare_new_motion = lambda: True
        panel._clear_move_thread_if_current = lambda: None
        panel._cancel_normal_jog_position_read = lambda: None
        delayed_reads = []
        panel._schedule_normal_jog_position_read = lambda: delayed_reads.append(True)
        panel._set_focus_position_var = lambda _position: None
        panel.log = lambda _message: None

        def external_stop(_speed):
            panel.stop_motion(log_action=False)
            return True

        panel.elevator.move_up = external_stop

        with mock.patch("src.microscope.onway_microscope_panel.threading.Thread", ImmediateThread):
            panel.start_hold_move("up")

        self.assertEqual(panel.elevator.stop_calls, 1)
        self.assertEqual(delayed_reads, [True])


if __name__ == "__main__":
    unittest.main()
