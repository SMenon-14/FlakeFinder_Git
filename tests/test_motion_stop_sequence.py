import threading
import unittest

from src.microscope.onway_microscope_panel import MicroscopeControlPanel


class ElevatorStub:
    def __init__(self, moving=False):
        self.is_connected = True
        self.moving = moving
        self.current_position = 12.345
        self.stop_calls = 0

    def is_motion_active(self):
        return self.moving

    def stop(self):
        self.stop_calls += 1
        self.moving = False
        return True


class MotionStopSequenceTests(unittest.TestCase):
    def _panel(self, moving=False):
        panel = object.__new__(MicroscopeControlPanel)
        panel.elevator = ElevatorStub(moving=moving)
        panel.move_stop_event = threading.Event()
        panel.move_thread = None
        panel.active_hold_direction = None
        panel._normal_jog_active = False
        panel._cancel_normal_jog_position_read = lambda: None
        panel._set_focus_position_var = lambda _position: None
        panel.log = lambda _message: None
        return panel

    def test_prepare_does_not_stop_an_idle_elevator_before_a_new_move(self):
        panel = self._panel(moving=False)

        self.assertTrue(panel._prepare_new_motion())

        self.assertEqual(panel.elevator.stop_calls, 0)
        self.assertFalse(panel.move_stop_event.is_set())

    def test_prepare_stops_an_active_elevator_before_a_new_move(self):
        panel = self._panel(moving=True)

        self.assertTrue(panel._prepare_new_motion())

        self.assertEqual(panel.elevator.stop_calls, 1)
        self.assertFalse(panel.move_stop_event.is_set())

    def test_stop_motion_sends_one_stop_sequence(self):
        panel = self._panel(moving=True)

        panel.stop_motion(log_action=False)

        self.assertEqual(panel.elevator.stop_calls, 1)
        self.assertTrue(panel.move_stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
