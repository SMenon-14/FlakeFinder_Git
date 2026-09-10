import threading
import unittest

from src.microscope.onway_microscope_panel import MicroscopeControlPanel


class FineJogElevatorStub:
    def __init__(self, position):
        self.is_connected = True
        self.current_position = position
        self.last_position_error = None
        self.moves = []

    def get_current_position(self):
        return self.current_position

    def move_to_position(self, target, **kwargs):
        self.moves.append((target, kwargs))
        self.current_position = target
        return True


class FineJogTests(unittest.TestCase):
    def _panel(self, position=50.0):
        panel = object.__new__(MicroscopeControlPanel)
        panel.elevator = FineJogElevatorStub(position)
        panel._set_focus_position_var = lambda _position: None
        panel.log = lambda _message: None
        return panel

    def test_fine_jog_uses_discrete_absolute_steps_and_own_settings(self):
        panel = self._panel()
        stop_event = threading.Event()

        self.assertTrue(panel._run_fine_jog_step("down", panel.FINE_JOG_SPEED, 60.0, stop_event))
        self.assertTrue(panel._run_fine_jog_step("up", panel.FINE_JOG_SPEED, 60.0, stop_event))

        first_target, first_kwargs = panel.elevator.moves[0]
        second_target, second_kwargs = panel.elevator.moves[1]
        self.assertAlmostEqual(first_target, 50.010)
        self.assertAlmostEqual(second_target, 50.000)
        self.assertEqual(first_kwargs["speed"], panel.FINE_JOG_SPEED)
        self.assertEqual(first_kwargs["arrival_tolerance_mm"], panel.FINE_JOG_ARRIVAL_TOLERANCE_MM)
        self.assertEqual(first_kwargs["max_attempts"], 1)
        self.assertTrue(first_kwargs["accept_settled_offset"])
        self.assertEqual(second_kwargs["current_pos"], 50.010)

    def test_fine_jog_clamps_to_low_limit(self):
        panel = self._panel(position=59.995)

        self.assertTrue(
            panel._run_fine_jog_step("down", panel.FINE_JOG_SPEED, 60.0, threading.Event())
        )

        self.assertAlmostEqual(panel.elevator.current_position, 60.0)


if __name__ == "__main__":
    unittest.main()
