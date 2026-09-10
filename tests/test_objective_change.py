import threading
import unittest

from src.microscope.onway_microscope_panel import MicroscopeControlPanel


class ObjectiveElevatorStub:
    def __init__(self, position):
        self.is_connected = True
        self.current_position = position
        self.last_position_error = None
        self.moves = []

    def get_current_position(self):
        return self.current_position

    def move_to_position(self, target, speed=32, current_pos=None, cancel_event=None):
        self.moves.append((target, speed, current_pos, cancel_event))
        self.current_position = target
        return True


class ObjectiveNosepieceStub:
    def __init__(self, lens):
        self.is_connected = True
        self.current_lens = lens
        self.commands = []

    def read_position(self):
        return self.current_lens

    def send_lens_command(self, lens):
        self.commands.append(lens)
        self.current_lens = None
        return True

    def wait_for_lens(self, lens, timeout_s, cancel_event):
        self.current_lens = lens
        return True


class ObjectiveChangeTests(unittest.TestCase):
    def _panel(self, position=50.0, lens=2):
        panel = object.__new__(MicroscopeControlPanel)
        panel.elevator = ObjectiveElevatorStub(position)
        panel.nosepiece = ObjectiveNosepieceStub(lens)
        panel.busy_lock = threading.Lock()
        panel.objective_transaction_lock = threading.Lock()
        panel.move_stop_event = threading.Event()
        panel._set_focus_position_var = lambda _position: None
        panel._set_current_lens_var = lambda _lens: None
        panel.log = lambda _message: None
        return panel

    def test_round_trip_uses_deterministic_absolute_targets(self):
        panel = self._panel()

        self.assertTrue(panel._perform_objective_change(1, 5.0, 12.0, 60.0))
        self.assertTrue(panel._perform_objective_change(2, 5.0, 12.0, 60.0))

        targets = [move[0] for move in panel.elevator.moves]
        speeds = [move[1] for move in panel.elevator.moves]
        for actual, expected in zip(targets, [45.0, 49.971, 45.0, 50.0]):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(speeds, [panel.OBJECTIVE_ELEVATOR_SPEED] * 4)
        self.assertEqual(panel.elevator.current_position, 50.0)


if __name__ == "__main__":
    unittest.main()
