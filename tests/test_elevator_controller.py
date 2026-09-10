import unittest
from unittest import mock

from src.microscope.onway_microscope_panel import ElevatorController


def position_response(position_mm: float) -> bytes:
    raw_position = int(round(position_mm * ElevatorController.POSITION_COUNTS_PER_MM))
    frame = [
        0xFF,
        ElevatorController.DEVICE_ADDRESS,
        0x00,
        ElevatorController.POSITION_RESPONSE_COMMAND,
        (raw_position >> 8) & 0xFF,
        raw_position & 0xFF,
    ]
    return bytes(frame + [ElevatorController.calculate_checksum(frame)])


def legacy_position_response(position_mm: float) -> bytes:
    raw_position = int(round(position_mm * ElevatorController.POSITION_COUNTS_PER_MM))
    frame = [
        0xFF,
        ElevatorController.DEVICE_ADDRESS,
        ElevatorController.LEGACY_QUERY_POSITION_COMMAND,
        (raw_position >> 16) & 0xFF,
        (raw_position >> 8) & 0xFF,
        raw_position & 0xFF,
    ]
    return bytes(frame + [ElevatorController.calculate_checksum(frame)])


class FakeElevatorSerial:
    def __init__(self, positions=(), max_read_size=None, query_protocol="standard",
                 ack_before_position=False, ack_only=False):
        self.positions = list(positions)
        self.max_read_size = max_read_size
        self.query_protocol = query_protocol
        self.ack_before_position = ack_before_position
        self.ack_only = ack_only
        self.writes = []
        self._rx = bytearray()
        self.is_open = True
        self.timeout = 0.01

    @property
    def in_waiting(self):
        return len(self._rx)

    def reset_input_buffer(self):
        self._rx.clear()

    def reset_output_buffer(self):
        pass

    def write(self, packet):
        packet = bytes(packet)
        self.writes.append(packet)
        standard_query = packet[2:4] == bytes((0x00, 0x53))
        legacy_query = packet[2:4] == bytes((0x5B, 0x00))
        if len(packet) == 7 and (
            (standard_query and self.query_protocol == "standard")
            or (legacy_query and self.query_protocol == "legacy")
        ):
            if self.ack_only:
                self._rx.extend(bytes.fromhex("FF 01 01 00 00 00 02"))
                return len(packet)
            if self.positions:
                position = self.positions.pop(0)
                if self.ack_before_position:
                    self._rx.extend(bytes.fromhex("FF 01 01 00 00 00 02"))
                response_factory = (
                    position_response if self.query_protocol == "standard"
                    else legacy_position_response
                )
                self._rx.extend(response_factory(position))
        return len(packet)

    def flush(self):
        pass

    def read(self, size=1):
        if self.max_read_size is not None:
            size = min(size, self.max_read_size)
        size = min(size, len(self._rx))
        data = bytes(self._rx[:size])
        del self._rx[:size]
        return data

    def close(self):
        self.is_open = False


def connected_controller(fake_serial, logger=None):
    controller = ElevatorController("COM_TEST", timeout=0.01, protocol_logger=logger)
    controller.ser = fake_serial
    controller.is_connected = True
    return controller


class ElevatorControllerProtocolTests(unittest.TestCase):
    def test_position_query_packet_and_fragmented_response(self):
        trace = []
        fake = FakeElevatorSerial([58.0], max_read_size=2)
        controller = connected_controller(fake, trace.append)

        with mock.patch("src.microscope.onway_microscope_panel.time.sleep"):
            measured = controller.get_current_position()

        self.assertEqual(measured, 58.0)
        self.assertEqual(fake.writes, [bytes.fromhex("FF 01 00 53 00 00 54")])
        self.assertTrue(any("[ELEVATOR TX] FF 01 00 53 00 00 54" in line for line in trace))
        self.assertTrue(any("[ELEVATOR RX]" in line for line in trace))

    def test_response_validation_uses_response_byte_and_two_position_bytes(self):
        response = position_response(58.0)

        self.assertEqual(response, bytes.fromhex("FF 01 00 5B E2 90 CE"))
        self.assertTrue(ElevatorController.is_valid_response_frame(response, expected_cmd=0x5B))
        self.assertEqual(ElevatorController.parse_position_response(response, 0x5B), 58.0)

        old_layout = bytearray(response)
        old_layout[2], old_layout[3] = 0x5B, 0x00
        old_layout[6] = sum(old_layout[1:6]) & 0xFF
        self.assertFalse(ElevatorController.is_valid_response_frame(bytes(old_layout), expected_cmd=0x5B))

    def test_general_ack_is_skipped_before_position_response(self):
        fake = FakeElevatorSerial([58.0], ack_before_position=True)
        controller = connected_controller(fake)

        with mock.patch("src.microscope.onway_microscope_panel.time.sleep"):
            measured = controller.get_current_position()

        self.assertEqual(measured, 58.0)
        self.assertEqual(fake.writes, [bytes.fromhex("FF 01 00 53 00 00 54")])
        self.assertEqual(controller._position_protocol, controller.POSITION_PROTOCOL_STANDARD)

    def test_ack_without_position_reports_controller_limitation(self):
        fake = FakeElevatorSerial(query_protocol="standard", ack_only=True)
        controller = connected_controller(fake)
        controller.POSITION_QUERY_TIMEOUT_S = 0.01

        measured = controller.get_current_position()

        self.assertIsNone(measured)
        self.assertIn("acknowledged the query but returned no position data",
                      controller.last_position_error)

    def test_position_read_retries_once_after_an_interfering_ack(self):
        fake = FakeElevatorSerial()
        controller = connected_controller(fake)
        attempts = []

        def query(clear_input=True):
            self.assertTrue(clear_input)
            attempts.append(True)
            if len(attempts) == 1:
                controller.last_position_error = (
                    "position query failed (legacy: controller acknowledged the query "
                    "but returned no position data)"
                )
                return None
            controller.current_position = 58.0
            controller.last_position_error = None
            return 58.0

        controller._query_current_position = query
        controller.wait_for_motion_idle = mock.Mock(return_value=True)

        with mock.patch("src.microscope.onway_microscope_panel.time.sleep"):
            measured = controller.get_current_position()

        self.assertEqual(measured, 58.0)
        self.assertEqual(len(attempts), 2)

    def test_vendor_query_fallback_is_detected_and_cached(self):
        fake = FakeElevatorSerial([58.0, 58.1], query_protocol="legacy")
        controller = connected_controller(fake)
        controller.POSITION_QUERY_TIMEOUT_S = 0.01

        first = controller.get_current_position()
        second = controller.get_current_position()

        self.assertEqual(first, 58.0)
        self.assertEqual(second, 58.1)
        self.assertEqual(controller._position_protocol, controller.POSITION_PROTOCOL_LEGACY)
        self.assertEqual(fake.writes, [
            bytes.fromhex("FF 01 00 53 00 00 54"),
            bytes.fromhex("FF 01 5B 00 00 00 5C"),
            bytes.fromhex("FF 01 5B 00 00 00 5C"),
        ])
        self.assertEqual(legacy_position_response(58.0), bytes.fromhex("FF 01 5B 00 E2 90 CE"))

    def test_absolute_move_polls_position_until_stably_at_target(self):
        fake = FakeElevatorSerial([50.0, 50.995, 51.004, 51.004])
        controller = connected_controller(fake)

        with mock.patch("src.microscope.onway_microscope_panel.time.sleep"):
            arrived = controller.position_control(51.0, timeout_s=1.0)

        self.assertTrue(arrived)
        self.assertAlmostEqual(controller.current_position, 51.004)
        self.assertEqual(fake.writes[0], bytes.fromhex("FF 01 00 4D C7 38 4D"))
        position_queries = [
            packet for packet in fake.writes
            if packet[2:4] == bytes((0x00, ElevatorController.QUERY_POSITION_COMMAND))
        ]
        self.assertEqual(position_queries, [
            bytes.fromhex("FF 01 00 53 00 00 54"),
            bytes.fromhex("FF 01 00 53 00 00 54"),
            bytes.fromhex("FF 01 00 53 00 00 54"),
        ])
        stop_packets = [
            packet for packet in fake.writes
            if packet == bytes.fromhex("FF 01 00 00 00 00 01")
        ]
        self.assertEqual(len(stop_packets), 2)
        self.assertFalse(any(packet[2:4] == bytes((0x00, 0x4F)) for packet in fake.writes))

    def test_absolute_target_must_fit_two_position_bytes(self):
        fake = FakeElevatorSerial()
        controller = connected_controller(fake)

        self.assertFalse(controller.position_control(65.536))
        self.assertEqual(fake.writes, [])
        self.assertIn("0.000-60.000 mm working range", controller.last_position_error)

    def test_absolute_move_corrects_from_the_settled_position(self):
        fake = FakeElevatorSerial([
            50.990,
            51.012,
            51.020,
            51.007,
            50.998,
            51.003,
        ])
        controller = connected_controller(fake)
        controller.current_position = 50.0
        controller._motion_settle_delay_s = 0.0
        controller._drain_stale_input = mock.Mock()

        with mock.patch("src.microscope.onway_microscope_panel.time.sleep"):
            arrived = controller.move_to_position(51.0, speed=32)

        self.assertTrue(arrived)
        self.assertAlmostEqual(controller.current_position, 50.998)
        position_commands = [
            packet for packet in fake.writes
            if packet[2:4] == bytes((0x00, ElevatorController.SET_POSITION_COMMAND))
        ]
        self.assertEqual(len(position_commands), 2)

    def test_settled_ten_micron_tolerance_accepts_seven_micron_residual(self):
        fake = FakeElevatorSerial([56.246, 56.246])
        controller = connected_controller(fake)
        controller.current_position = 56.246
        controller._motion_settle_delay_s = 0.0
        controller._drain_stale_input = mock.Mock()

        with mock.patch("src.microscope.onway_microscope_panel.time.sleep"):
            arrived = controller.move_to_position(56.253, speed=8)

        self.assertTrue(arrived)
        self.assertAlmostEqual(controller.current_position, 56.246)
        position_queries = [
            packet for packet in fake.writes
            if packet[2:4] == bytes((0x00, ElevatorController.QUERY_POSITION_COMMAND))
        ]
        self.assertEqual(len(position_queries), 2)

    def test_absolute_target_must_stay_inside_working_range(self):
        fake = FakeElevatorSerial()
        controller = connected_controller(fake)

        self.assertFalse(controller.position_control(60.001))
        self.assertFalse(controller.position_control(-0.001))
        self.assertEqual(fake.writes, [])
        self.assertIn("0.000-60.000 mm working range", controller.last_position_error)


if __name__ == "__main__":
    unittest.main()
