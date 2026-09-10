"""Focus/elevator + objective nosepiece controllers and MicroscopeControlPanel."""
import os
import re
import json
import time
import struct
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import Callable, Optional
from src.microscope.autofocus import FocusCalculator
from src.paths import CONFIGS_DIR

import serial
import serial.tools.list_ports
import pyautogui
import pyperclip

FOCUS_BAUDRATE = 9600
OBJECTIVE_BAUDRATE = 115200
AUTOFOCUS_LENS_CONFIG_PATH = CONFIGS_DIR / 'autofocus_lens_config.json'

class ElevatorController:
    """Serial controller for microscope Z/elevator with framed reads and soft position tracking."""

    DEVICE_ADDRESS = 0x01
    QUERY_POSITION_COMMAND = 0x53
    LEGACY_QUERY_POSITION_COMMAND = 0x5B
    POSITION_RESPONSE_COMMAND = 0x5B
    SET_POSITION_COMMAND = 0x4D
    POSITION_PROTOCOL_STANDARD = "standard"
    POSITION_PROTOCOL_LEGACY = "legacy"
    POSITION_COUNTS_PER_MM = 1000.0
    MIN_POSITION_MM = 0.0
    MAX_POSITION_MM = 60.0
    POSITION_QUERY_TIMEOUT_S = 0.8
    POSITION_POLL_INTERVAL_S = 0.05
    POSITION_ARRIVAL_TOLERANCE_MM = 0.01
    POSITION_SETTLED_TOLERANCE_MM = 0.01
    POSITION_CORRECTION_SPEED = 8
    POSITION_MAX_ATTEMPTS = 3

    def __init__(self, port: str, baudrate: int = FOCUS_BAUDRATE, timeout: float = 1.0,
                 protocol_logger: Optional[Callable[[str], None]] = None):
        self.ser: Optional[serial.Serial] = None
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.protocol_logger = protocol_logger
        self.is_connected = False
        self.current_position: Optional[float] = None
        self.last_position_error: Optional[str] = None
        self._position_protocol: Optional[str] = None
        self._lock = threading.RLock()
        self._transaction_lock = threading.RLock()
        self._rx_buffer = bytearray()
        self._motion_state_lock = threading.Lock()
        self._move_in_progress = False
        self._last_motion_end = 0.0
        self._motion_settle_delay_s = 0.35
        self._pending_drain = False

    def connect(self) -> bool:
        try:
            self.ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
            )
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            self._rx_buffer.clear()
            self.is_connected = True
            return True
        except Exception:
            self.is_connected = False
            return False

    def disconnect(self):
        with self._lock:
            if self.ser and self.ser.is_open:
                self.ser.close()
        self.is_connected = False
        self.current_position = None
        self._rx_buffer.clear()
        with self._motion_state_lock:
            self._move_in_progress = False
            self._last_motion_end = 0.0

    def _set_motion_active(self, active: bool):
        with self._motion_state_lock:
            previous = self._move_in_progress
            self._move_in_progress = active
            if active:
                self._last_motion_end = 0.0
                self._pending_drain = True
            elif previous:
                self._last_motion_end = time.time()

    def is_motion_active(self) -> bool:
        """Return the host-side motion state without issuing a serial command."""
        with self._motion_state_lock:
            return self._move_in_progress

    def _drain_stale_input(self, window_s: float = 0.2, hard_cap_s: float = 0.6):
        """Absorb and discard bytes still trickling in from a just-finished
        move/stop command (this controller sends delayed ack frames for those
        that otherwise land in the middle of the next position query's read
        window and get misread as a corrupt/unexpected response)."""
        if not self.is_connected or self.ser is None:
            return
        try:
            with self._lock:
                start = time.time()
                deadline = start + window_s
                hard_deadline = start + hard_cap_s
                drained_any = False
                while time.time() < deadline and time.time() < hard_deadline:
                    waiting = self.ser.in_waiting if hasattr(self.ser, "in_waiting") else 0
                    if waiting > 0:
                        chunk = self.ser.read(waiting)
                        if chunk:
                            drained_any = True
                            self._trace_protocol("RX", bytes(chunk), "stale, discarded")
                            deadline = min(time.time() + window_s, hard_deadline)
                    else:
                        time.sleep(0.02)
                if hasattr(self.ser, "reset_input_buffer"):
                    self.ser.reset_input_buffer()
                self._rx_buffer.clear()
                if drained_any:
                    self._trace_protocol("DRAIN", b"", "cleared stale bytes before position query")
        except Exception:
            pass

    def wait_for_motion_idle(self, timeout: float = 12.0) -> bool:
        deadline = time.time() + max(timeout, 0.1)
        while time.time() < deadline:
            with self._motion_state_lock:
                moving = self._move_in_progress
                last_motion_end = self._last_motion_end
            if not moving:
                if last_motion_end == 0.0 or (time.time() - last_motion_end) >= self._motion_settle_delay_s:
                    if self._pending_drain:
                        self._drain_stale_input()
                        self._pending_drain = False
                    return True
            time.sleep(0.05)
        return False

    @staticmethod
    def calculate_checksum(data: list[int]) -> int:
        if len(data) != 6:
            raise ValueError("Checksum calculation requires 6 data bytes")
        return sum(data[1:6]) & 0xFF

    @staticmethod
    def validate_response_checksum(response: bytes) -> bool:
        if len(response) != 7:
            return False
        return response[6] == (sum(response[1:6]) & 0xFF)

    @classmethod
    def is_valid_response_frame(cls, response: bytes, expected_cmd: Optional[int] = None) -> bool:
        if len(response) != 7:
            return False
        if response[0] != 0xFF or response[1] != cls.DEVICE_ADDRESS:
            return False
        if expected_cmd is not None:
            if response[2] != 0x00 or response[3] != expected_cmd:
                return False
        return cls.validate_response_checksum(response)

    def _trace_protocol(self, direction: str, payload: bytes, detail: str = ""):
        if self.protocol_logger is None:
            return
        suffix = f" ({detail})" if detail else ""
        message = f"[ELEVATOR {direction}] {' '.join(f'{byte:02X}' for byte in payload)}{suffix}"
        try:
            self.protocol_logger(message)
        except Exception:
            pass

    @staticmethod
    def parse_position(response: bytes) -> float:
        if len(response) != 7:
            raise ValueError("Response must be 7 bytes")
        if response[0] != 0xFF or response[1] != ElevatorController.DEVICE_ADDRESS:
            raise ValueError("Invalid response header")
        if not ElevatorController.validate_response_checksum(response):
            raise ValueError("Invalid response checksum")
        raw_position = (response[4] << 8) | response[5]
        return raw_position / ElevatorController.POSITION_COUNTS_PER_MM

    @classmethod
    def parse_position_response(cls, response: bytes, expected_cmd: int) -> float:
        if not cls.is_valid_response_frame(response, expected_cmd):
            raise ValueError(f"Unexpected position response frame: {[hex(b) for b in response]}")
        return cls.parse_position(response)

    @classmethod
    def is_valid_legacy_position_frame(cls, response: bytes) -> bool:
        return (
            len(response) == 7
            and response[0] == 0xFF
            and response[1] == cls.DEVICE_ADDRESS
            and response[2] == cls.LEGACY_QUERY_POSITION_COMMAND
            and cls.validate_response_checksum(response)
        )

    @classmethod
    def is_general_ack_frame(cls, response: bytes) -> bool:
        return (
            len(response) == 7
            and response[:6] == bytes((0xFF, cls.DEVICE_ADDRESS, 0x01, 0x00, 0x00, 0x00))
            and cls.validate_response_checksum(response)
        )

    @classmethod
    def parse_legacy_position_response(cls, response: bytes) -> float:
        if not cls.is_valid_legacy_position_frame(response):
            raise ValueError(f"Unexpected legacy position frame: {[hex(b) for b in response]}")
        raw_position = (response[3] << 16) | (response[4] << 8) | response[5]
        if raw_position & 0x800000:
            raw_position -= 0x1000000
        return raw_position / cls.POSITION_COUNTS_PER_MM

    def send_command(self, cmd_bytes: list[int], wait_response: bool = False,
                     expected_response_cmd: Optional[int] = None,
                     response_validator: Optional[Callable[[bytes], bool]] = None,
                     clear_input: bool = False,
                     clear_output: bool = False,
                     response_timeout: Optional[float] = None) -> tuple[bool, Optional[bytes]]:
        if not self.is_connected or self.ser is None:
            return False, None
        packet = b""
        try:
            checksum = self.calculate_checksum(cmd_bytes)
            packet = bytes(cmd_bytes + [checksum])
            with self._lock:
                if clear_input:
                    self.ser.reset_input_buffer()
                    self._rx_buffer.clear()
                if clear_output and hasattr(self.ser, "reset_output_buffer"):
                    self.ser.reset_output_buffer()
                self._trace_protocol("TX", packet)
                self.ser.write(packet)
                self.ser.flush()
                response = None
                if wait_response:
                    time.sleep(0.05)
                    response = self.read_response(
                        expected_cmd=expected_response_cmd,
                        response_validator=response_validator,
                        timeout=response_timeout,
                    )
            return True, response
        except Exception as exc:
            self.last_position_error = f"serial command failed: {exc}"
            self._trace_protocol("ERROR", packet, str(exc))
            return False, None

    def _extract_frame_from_buffer(self) -> Optional[bytes]:
        while True:
            header_index = self._rx_buffer.find(b"\xFF\x01")
            if header_index < 0:
                if len(self._rx_buffer) > 1:
                    self._rx_buffer.clear()
                return None
            if header_index > 0:
                del self._rx_buffer[:header_index]
            if len(self._rx_buffer) < 7:
                return None
            frame = bytes(self._rx_buffer[:7])
            del self._rx_buffer[:7]
            return frame

    def _read_frame(self, deadline: float) -> Optional[bytes]:
        while time.time() < deadline:
            frame = self._extract_frame_from_buffer()
            if frame is not None:
                return frame

            if hasattr(self.ser, "in_waiting"):
                waiting = self.ser.in_waiting
                if waiting <= 0:
                    time.sleep(min(0.01, max(0.0, deadline - time.time())))
                    continue
                read_len = min(waiting, 64)
            else:
                read_len = 1
            chunk = self.ser.read(read_len)
            if chunk:
                self._trace_protocol("RX", bytes(chunk))
                self._rx_buffer.extend(chunk)
            else:
                time.sleep(0.01)
        return self._extract_frame_from_buffer()

    def read_response(self, expected_cmd: Optional[int] = None,
                      response_validator: Optional[Callable[[bytes], bool]] = None,
                      timeout: Optional[float] = None) -> Optional[bytes]:
        if not self.is_connected or self.ser is None:
            self.last_position_error = "not connected to elevator controller"
            return None
        last_rejected: Optional[bytes] = None
        try:
            if timeout is None:
                timeout = self.timeout + 0.2
            deadline = time.time() + max(timeout, 0.1)
            with self._lock:
                while time.time() < deadline:
                    response = self._read_frame(deadline)
                    if response is None:
                        break
                    if response_validator is not None:
                        accepted = response_validator(response)
                    else:
                        accepted = expected_cmd is None or self.is_valid_response_frame(
                            response,
                            expected_cmd,
                        )
                    if accepted:
                        self.last_position_error = None
                        return response
                    last_rejected = response
        except Exception as exc:
            self.last_position_error = f"exception while reading response: {exc}"
            return None
        if last_rejected is not None:
            if response_validator is not None and self.is_general_ack_frame(last_rejected):
                self.last_position_error = (
                    "controller acknowledged the query but returned no position data"
                )
                return None
            if not self.validate_response_checksum(last_rejected):
                reason = "bad checksum"
            elif expected_cmd is not None:
                reason = (
                    f"unexpected response bytes 0x{last_rejected[2]:02X} "
                    f"0x{last_rejected[3]:02X} (expected 0x00 0x{expected_cmd:02X})"
                )
            else:
                reason = "frame rejected"
            self.last_position_error = (
                f"received frame did not validate ({reason}): "
                f"{' '.join(f'{b:02X}' for b in last_rejected)}"
            )
        else:
            self.last_position_error = "no data received from elevator controller within timeout"
        return None

    def move_up(self, speed: int = 32) -> bool:
        if not (0 <= speed <= 64):
            return False
        success = self.send_command([0xFF, 0x01, 0x00, 0x08, 0x00, speed], clear_output=True)[0]
        if success:
            self._set_motion_active(True)
        return success

    def move_down(self, speed: int = 32) -> bool:
        if not (0 <= speed <= 64):
            return False
        success = self.send_command([0xFF, 0x01, 0x00, 0x10, 0x00, speed], clear_output=True)[0]
        if success:
            self._set_motion_active(True)
        return success

    def stop(self) -> bool:
        if not self.is_connected or self.ser is None:
            return False
        stop_cmd = [0xFF, 0x01, 0x00, 0x00, 0x00, 0x00]
        stop_packet = bytes(stop_cmd + [self.calculate_checksum(stop_cmd)])
        success = False
        try:
            with self._lock:
                if hasattr(self.ser, "reset_output_buffer"):
                    self.ser.reset_output_buffer()
                self._trace_protocol("TX", stop_packet, "stop 1/2")
                self.ser.write(stop_packet)
                self.ser.flush()
                time.sleep(0.02)
                self._trace_protocol("TX", stop_packet, "stop 2/2")
                self.ser.write(stop_packet)
                self.ser.flush()
                if hasattr(self.ser, "reset_input_buffer"):
                    self.ser.reset_input_buffer()
                self._rx_buffer.clear()
                # Keep the serial lock until delayed Stop acknowledgements
                # and position frames have been drained.  Otherwise a
                # concurrent fine-focus read can send its query in this gap
                # and consume the Stop acknowledgement as the query reply.
                self._drain_stale_input()
                self._set_motion_active(False)
                with self._motion_state_lock:
                    self._pending_drain = False
            success = True
        except Exception as exc:
            self._trace_protocol("ERROR", stop_packet, str(exc))
            success = False
        return success

    def set_absolute_speed(self, speed_um: int) -> bool:
        speed_bytes = struct.pack(">h", speed_um)
        return self.send_command([0xFF, 0x01, 0x00, 0x18, speed_bytes[0], speed_bytes[1]])[0]

    def position_control(self, target_position_mm: float,
                         cancel_event: Optional[threading.Event] = None,
                         timeout_s: float = 10.0,
                         approach_from_mm: Optional[float] = None,
                         arrival_tolerance_mm: Optional[float] = None,
                          settled_tolerance_mm: Optional[float] = None,
                          accept_settled_offset: bool = False) -> bool:
        if not self.MIN_POSITION_MM <= target_position_mm <= self.MAX_POSITION_MM:
            self.last_position_error = (
                f"target position {target_position_mm:.3f} mm is outside the "
                f"{self.MIN_POSITION_MM:.3f}-{self.MAX_POSITION_MM:.3f} mm working range"
            )
            return False
        arrival_tolerance_mm = (
            self.POSITION_ARRIVAL_TOLERANCE_MM
            if arrival_tolerance_mm is None
            else arrival_tolerance_mm
        )
        settled_tolerance_mm = (
            self.POSITION_SETTLED_TOLERANCE_MM
            if settled_tolerance_mm is None
            else settled_tolerance_mm
        )
        if arrival_tolerance_mm <= 0 or settled_tolerance_mm <= 0:
            self.last_position_error = "position tolerances must be greater than 0 mm"
            return False

        position_counts = int(round(target_position_mm * self.POSITION_COUNTS_PER_MM))
        if not 0 <= position_counts <= 0xFFFF:
            self.last_position_error = (
                f"target position {target_position_mm:.3f} mm is outside the controller's "
                f"0.000-{0xFFFF / self.POSITION_COUNTS_PER_MM:.3f} mm range"
            )
            return False
        cmd = [
            0xFF,
            self.DEVICE_ADDRESS,
            0x00,
            self.SET_POSITION_COMMAND,
            (position_counts >> 8) & 0xFF,
            position_counts & 0xFF,
        ]
        if cancel_event is not None and cancel_event.is_set():
            return False
        self._set_motion_active(True)
        arrived = False
        try:
            with self._transaction_lock:
                success, _ = self.send_command(cmd)
                if not success:
                    return False

                # This controller does not autonomously stop at the target it
                # was given - confirmed by observation: an absolute-position
                # move kept cruising straight through the commanded target at
                # the jog speed (overshot by >1.5 mm on a 5 mm move) until
                # something else intervened. So arrival must be enforced from
                # the host side: stop the instant the measured position
                # reaches or passes the target, rather than waiting for two
                # consecutive polls to land inside the tolerance window on
                # their own - at this speed and poll rate that window is
                # narrower than the per-poll travel distance, so it never
                # would have.
                approach_position = approach_from_mm if approach_from_mm is not None else self.current_position
                direction_sign = 0
                if approach_position is not None:
                    if target_position_mm > approach_position:
                        direction_sign = 1
                    elif target_position_mm < approach_position:
                        direction_sign = -1

                deadline = time.monotonic() + max(timeout_s, 0.0)
                last_measured: Optional[float] = None
                while time.monotonic() < deadline:
                    if cancel_event is not None and cancel_event.is_set():
                        self.stop()
                        return False

                    measured = self._query_current_position(clear_input=True)
                    if measured is not None:
                        last_measured = measured
                        remaining = target_position_mm - measured
                        reached = abs(remaining) <= arrival_tolerance_mm
                        overshot = direction_sign != 0 and (remaining * direction_sign) < 0
                        if reached or overshot:
                            self.stop()
                            if not self.wait_for_motion_idle():
                                self.last_position_error = (
                                    "timed out waiting for motion to settle after stopping at "
                                    f"target {target_position_mm:.3f} mm"
                                )
                                return False
                            settled = self._query_current_position(clear_input=True)
                            if settled is None:
                                self.last_position_error = (
                                    "could not verify the settled position after stopping at "
                                    f"target {target_position_mm:.3f} mm"
                                )
                                return False
                            settled_error = target_position_mm - settled
                            if abs(settled_error) <= settled_tolerance_mm:
                                self.current_position = settled
                                self.last_position_error = None
                                arrived = True
                                return True
                            if accept_settled_offset:
                                self.current_position = settled
                                self.last_position_error = None
                                arrived = True
                                return True
                            self.current_position = settled
                            self.last_position_error = (
                                f"settled at {settled:.3f} mm, "
                                f"{abs(settled_error):.3f} mm from target "
                                f"{target_position_mm:.3f} mm"
                            )
                            return False
                    time.sleep(self.POSITION_POLL_INTERVAL_S)

                self.stop()
                measured_detail = (
                    f"; last measured position was {last_measured:.3f} mm"
                    if last_measured is not None else "; no valid position response was received"
                )
                self.last_position_error = (
                    f"position arrival timed out for target {target_position_mm:.3f} mm"
                    f"{measured_detail}"
                )
                return False
        finally:
            if not arrived and self.current_position is None:
                self.current_position = None
            self._set_motion_active(False)

    def _position_query_profiles(self) -> list[str]:
        profiles = [self.POSITION_PROTOCOL_STANDARD, self.POSITION_PROTOCOL_LEGACY]
        if self._position_protocol in profiles:
            profiles.remove(self._position_protocol)
            profiles.insert(0, self._position_protocol)
        return profiles

    def _position_query_packet(self, profile: str) -> list[int]:
        if profile == self.POSITION_PROTOCOL_STANDARD:
            return [
                0xFF,
                self.DEVICE_ADDRESS,
                0x00,
                self.QUERY_POSITION_COMMAND,
                0x00,
                0x00,
            ]
        return [
            0xFF,
            self.DEVICE_ADDRESS,
            self.LEGACY_QUERY_POSITION_COMMAND,
            0x00,
            0x00,
            0x00,
        ]

    def _parse_position_frame(self, response: bytes) -> float:
        if self.is_valid_response_frame(response, self.POSITION_RESPONSE_COMMAND):
            return self.parse_position_response(response, self.POSITION_RESPONSE_COMMAND)
        if self.is_valid_legacy_position_frame(response):
            return self.parse_legacy_position_response(response)
        raise ValueError(
            "response is neither a standard 00 5B frame nor a vendor-style 5B frame"
        )

    def _query_current_position(self, clear_input: bool = True) -> Optional[float]:
        errors = []
        for attempt_index, profile in enumerate(self._position_query_profiles()):
            success, response = self.send_command(
                self._position_query_packet(profile),
                wait_response=True,
                # Controllers commonly acknowledge a query with a general
                # FF 01 01 00 00 00 02 frame before sending position. Keep
                # reading until one of the two known position layouts arrives.
                response_validator=lambda frame: (
                    self.is_valid_response_frame(frame, self.POSITION_RESPONSE_COMMAND)
                    or self.is_valid_legacy_position_frame(frame)
                ),
                # Clear stale data once before the transaction, but never
                # between fallback attempts: a delayed position reply must not
                # be erased just before the second query.
                clear_input=clear_input and attempt_index == 0,
                response_timeout=self.POSITION_QUERY_TIMEOUT_S,
            )
            if not success:
                errors.append(f"{profile}: serial write failed")
                continue
            if response is None:
                errors.append(f"{profile}: {self.last_position_error or 'no response'}")
                continue
            try:
                position = self._parse_position_frame(response)
            except Exception as exc:
                errors.append(
                    f"{profile}: {exc} ({' '.join(f'{byte:02X}' for byte in response)})"
                )
                continue

            self._position_protocol = profile
            self.current_position = position
            self.last_position_error = None
            return position

        attempted = "; ".join(errors) if errors else "no query profile was attempted"
        self.last_position_error = f"position query failed ({attempted})"
        return None

    def get_current_position(self) -> Optional[float]:
        with self._transaction_lock:
            for attempt in range(2):
                if not self.wait_for_motion_idle():
                    self.last_position_error = (
                        "timed out waiting for motion to settle before querying position"
                    )
                    return None
                position = self._query_current_position(clear_input=True)
                if position is not None:
                    return position

                query_error = self.last_position_error or ""
                interrupted_by_ack = (
                    "acknowledged the query but returned no position data" in query_error
                )
                if attempt == 0 and interrupted_by_ack:
                    # A safety Stop is allowed to interrupt an in-flight
                    # position read.  Once its acknowledgement is gone, retry
                    # the complete read exactly once.
                    time.sleep(0.1)
                    continue
                return None
            return None

    def move_to_position(self, target_position_mm: float, speed: int = 32,
                         current_pos: Optional[float] = None,
                         cancel_event: Optional[threading.Event] = None,
                         arrival_tolerance_mm: Optional[float] = None,
                          settled_tolerance_mm: Optional[float] = None,
                          correction_speed: Optional[int] = None,
                         max_attempts: Optional[int] = None,
                         accept_settled_offset: bool = False) -> bool:
        if not self.MIN_POSITION_MM <= target_position_mm <= self.MAX_POSITION_MM:
            self.last_position_error = (
                f"target position {target_position_mm:.3f} mm is outside the "
                f"{self.MIN_POSITION_MM:.3f}-{self.MAX_POSITION_MM:.3f} mm working range"
            )
            return False
        if current_pos is None:
            current_pos = self.current_position
        if current_pos is None:
            current_pos = self.get_current_position()
        if current_pos is None:
            return False
        if abs(target_position_mm - current_pos) < 0.001:
            self.current_position = current_pos
            return True

        if cancel_event is not None and cancel_event.is_set():
            return False
        correction_speed = (
            self.POSITION_CORRECTION_SPEED
            if correction_speed is None
            else correction_speed
        )
        max_attempts = self.POSITION_MAX_ATTEMPTS if max_attempts is None else max_attempts
        settled_tolerance_mm = (
            self.POSITION_SETTLED_TOLERANCE_MM
            if settled_tolerance_mm is None
            else settled_tolerance_mm
        )
        if correction_speed <= 0 or max_attempts < 1 or settled_tolerance_mm <= 0:
            self.last_position_error = "invalid position correction settings"
            return False

        for attempt in range(max_attempts):
            if cancel_event is not None and cancel_event.is_set():
                return False

            attempt_speed = speed if attempt == 0 else min(speed, correction_speed)
            # Verified against a live lift move: negative drives toward smaller
            # position values (up toward 0), while positive drives down toward
            # larger values.
            speed_um = (
                -attempt_speed * 10
                if target_position_mm < current_pos
                else attempt_speed * 10
            )
            if not self.set_absolute_speed(speed_um):
                return False
            time.sleep(0.1)
            expected_travel_s = (
                abs(target_position_mm - current_pos) * 1000.0 / max(abs(speed_um), 1)
            )
            arrival_timeout_s = max(10.0, expected_travel_s + 5.0)
            if self.position_control(
                target_position_mm,
                cancel_event=cancel_event,
                timeout_s=arrival_timeout_s,
                approach_from_mm=current_pos,
                arrival_tolerance_mm=arrival_tolerance_mm,
                settled_tolerance_mm=settled_tolerance_mm,
                accept_settled_offset=accept_settled_offset,
            ):
                return True

            if cancel_event is not None and cancel_event.is_set():
                return False
            if self.current_position is None:
                return False
            current_pos = self.current_position
            if abs(target_position_mm - current_pos) <= settled_tolerance_mm:
                self.last_position_error = None
                return True

        self.last_position_error = (
            f"could not settle within {settled_tolerance_mm:.3f} mm "
            f"of target {target_position_mm:.3f} mm after "
            f"{max_attempts} attempts; last position was "
            f"{current_pos:.3f} mm"
        )
        return False

    def move_up_for_time(self, speed: int, duration: float) -> bool:
        if not (0 <= speed <= 64) or duration <= 0:
            return False
        start = time.time()
        self._set_motion_active(True)
        try:
            deadline = start + duration
            while time.time() < deadline:
                if not self.move_up(speed):
                    self.stop()
                    return False
                time.sleep(0.05)
            self.stop()
            elapsed = time.time() - start
            if self.current_position is not None:
                self.current_position = max(0.0, self.current_position - (speed * 10) * elapsed / 1000.0)
            return True
        finally:
            self._set_motion_active(False)

    def move_down_for_time(self, speed: int, duration: float) -> bool:
        if not (0 <= speed <= 64) or duration <= 0:
            return False
        start = time.time()
        self._set_motion_active(True)
        try:
            deadline = start + duration
            while time.time() < deadline:
                if not self.move_down(speed):
                    self.stop()
                    return False
                time.sleep(0.05)
            self.stop()
            elapsed = time.time() - start
            if self.current_position is not None:
                self.current_position += (speed * 10) * elapsed / 1000.0
            return True
        finally:
            self._set_motion_active(False)


class NosepieceController:
    """Serial controller for motorized objective nosepiece."""

    LENS_COMMANDS: dict[int, bytes] = {
        1: bytes.fromhex("5A A5 06 83 10 03 01 00 01 9E"),
        2: bytes.fromhex("5A A5 06 83 10 03 01 00 02 9F"),
        3: bytes.fromhex("5A A5 06 83 10 03 01 00 03 A0"),
        4: bytes.fromhex("5A A5 06 83 10 03 01 00 04 A1"),
        5: bytes.fromhex("5A A5 06 83 10 03 01 00 05 A2"),
    }

    MAG_LABELS: dict[int, str] = {1: "5×", 2: "10×", 3: "20×", 4: "40×", 5: "50×"}

    def __init__(self, port: str, baudrate: int = OBJECTIVE_BAUDRATE, timeout: float = 1.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser: Optional[serial.Serial] = None
        self.is_connected = False
        self._lock = threading.Lock()
        self._rx_buffer = bytearray()
        self.current_lens: Optional[int] = None

    def connect(self) -> bool:
        try:
            self.ser = serial.Serial(self.port, self.baudrate, timeout=min(self.timeout, 0.05))
            self.is_connected = True
            self._rx_buffer.clear()
            return True
        except Exception:
            self.is_connected = False
            return False

    def disconnect(self):
        with self._lock:
            if self.ser and self.ser.is_open:
                self.ser.close()
        self.is_connected = False
        self._rx_buffer.clear()
        self.current_lens = None

    def send_lens_command(self, lens_pos: int) -> bool:
        if not self.is_connected or self.ser is None or lens_pos not in self.LENS_COMMANDS:
            return False
        try:
            with self._lock:
                # Once a physical move is attempted, the cached position is no
                # longer trustworthy until a status frame confirms a lens.
                self.current_lens = None
                self.ser.write(self.LENS_COMMANDS[lens_pos])
                self.ser.flush()
            return True
        except Exception:
            return False

    @staticmethod
    def parse_position(response_bytes: bytes) -> Optional[int]:
        """Parse vendor objective-status frame."""
        if len(response_bytes) < 7:
            return None

        frame = response_bytes[-7:]
        if frame[0:6] == bytes.fromhex("5A A5 03 82 4F 4B") and frame[6] in range(1, 6):
            return frame[6]
        return None

    def read_position(self) -> Optional[int]:
        if not self.is_connected or self.ser is None:
            return None
        try:
            with self._lock:
                waiting = self.ser.in_waiting if hasattr(self.ser, "in_waiting") else 0
                if waiting <= 0:
                    return None
                response = self.ser.read(min(waiting, 64))
                if not response:
                    return None
                self._rx_buffer.extend(response)
                if len(self._rx_buffer) > 64:
                    del self._rx_buffer[:-64]
                packet = bytes(self._rx_buffer)
            pos = self.parse_position(packet)
            if pos is not None:
                self.current_lens = pos
            return pos
        except Exception:
            return None

    def wait_for_lens(self, target_lens: int, timeout_s: float = 12.0, poll_interval: float = 0.3,
                      cancel_event: Optional[threading.Event] = None) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return False
            pos = self.read_position()
            if pos == target_lens:
                return True
            time.sleep(poll_interval)
        return False


class MicroscopeControlPanel(ttk.LabelFrame):
    """Focus/elevator + objective control, embedded inside the main app."""

    LOG_ELEVATOR_PROTOCOL = False
    MEDIUM_ELEVATOR_SPEED = 32
    OBJECTIVE_ELEVATOR_SPEED = 64
    FINE_ELEVATOR_SPEED = 8
    FINE_JOG_SPEED = 4
    FINE_JOG_STEP_MM = 0.010
    FINE_JOG_ARRIVAL_TOLERANCE_MM = 0.003
    NORMAL_JOG_LIMIT_GUARD_MM = 0.5
    NORMAL_JOG_IDLE_READ_DELAY_S = 3.0
    NORMAL_JOG_COMMAND_INTERVAL_S = 0.05
    OBJECTIVE_POLL_INTERVAL_S = 1.5
    OBJECTIVE_CONNECT_READ_TIMEOUT_S = 2.0
    OBJECTIVE_READ_RETRY_INTERVAL_S = 0.05
    DEFAULT_OBJECTIVE_LENS = 2  # 10x
    STARTUP_OBJECTIVE_MOVE_MM = 5.0
    OBJECTIVE_FOCUS_OFFSETS_MM: dict[int, float] = {
        1: -0.029,  # 5x
        2: 0.000,   # 10x reference
        3: -0.036,  # 20x
        4: 0.000,   # 40x: no measured correction yet
        5: 0.044,   # 50x
    }

    AUTOFOCUS_START_HEIGHT_MM = 58.0
    AUTOFOCUS_BRENNER_STEP_MM = 0.07
    AUTOFOCUS_BRENNER_PATIENCE = 20
    AUTOFOCUS_BRENNER_GOOD_SCORE = 8_000_000
    AUTOFOCUS_BRENNER_RELATIVE_GAIN = 0.25
    AUTOFOCUS_LAPLACIAN_STEP_MM = 0.005
    AUTOFOCUS_LAPLACIAN_PATIENCE = 5
    AUTOFOCUS_MAX_ITERATIONS = 100
    AUTOFOCUS_SETTLE_DELAY_S = 0.5

    def __init__(self, master, show_connections: bool = True):
        super().__init__(master, text="Focus / Objective", padding=6)
        self.show_connections = show_connections
        self.elevator: Optional[ElevatorController] = None
        self.nosepiece: Optional[NosepieceController] = None
        self.move_thread: Optional[threading.Thread] = None
        self.move_stop_event = threading.Event()
        self.poll_stop_event = threading.Event()
        self.busy_lock = threading.Lock()
        self.objective_transaction_lock = threading.Lock()
        self._startup_objective_lock = threading.Lock()
        self._startup_objective_active = False
        self.active_hold_direction: Optional[str] = None
        self._normal_jog_active = False
        self._normal_jog_read_lock = threading.Lock()
        self._normal_jog_read_timer: Optional[threading.Timer] = None
        self._normal_jog_read_generation = 0

        self.elev_port_var = tk.StringVar(value="COM8")
        self.obj_port_var = tk.StringVar(value="COM4")
        self.speed_var = tk.IntVar(value=self.MEDIUM_ELEVATOR_SPEED)
        self.safe_home_distance_mm_var = tk.DoubleVar(value=5.0)
        self.objective_lift_distance_mm_var = tk.DoubleVar(value=1.0)
        self.objective_change_timeout_var = tk.DoubleVar(value=12.0)
        self.focus_low_limit_var = tk.StringVar(value=f"{ElevatorController.MAX_POSITION_MM:.3f}")
        self.current_focus_var = tk.StringVar(value="-- mm")
        self.focus_set_position_var = tk.StringVar(value="")
        self.current_lens_var = tk.StringVar(value="--")
        self.focus_low_limit_mm: Optional[float] = ElevatorController.MAX_POSITION_MM
        self._last_lens_display: Optional[str] = "--"
        self._closing = False
        self._ui_after_ids: dict[str, str] = {}
        self._objective_init_popup: Optional[tk.Toplevel] = None
        self._objective_init_progress: Optional[ttk.Progressbar] = None
        self._objective_init_status_var: Optional[tk.StringVar] = None
        self.focus_low_limit_var.trace_add("write", self._on_focus_low_limit_var_changed)
        self.focus_calculator = FocusCalculator(step_size=0.5)

        self._build_ui()
        self.start_objective_polling()

    # ───────────────────────── UI ─────────────────────────
    def _build_ui(self):
        self.columnconfigure(0, weight=1)

        # Focus / elevator
        focus = ttk.Frame(self, padding=(0, 4))
        focus.grid(row=0, column=0, sticky="ew")
        focus.columnconfigure(0, weight=1)
        focus.columnconfigure(1, weight=1)

        speed_box = ttk.LabelFrame(focus, text="Focus")
        speed_box.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        speed_box.columnconfigure(0, weight=1)
        ttk.Scale(speed_box, from_=0, to=64, variable=self.speed_var, orient="horizontal").grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 2))

        move_box = ttk.Frame(speed_box)
        move_box.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 4))
        move_box.columnconfigure(0, weight=1)
        move_box.columnconfigure(1, weight=0)
        self.up_btn = ttk.Button(move_box, text="Up")
        self.up_btn.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.fine_up_btn = ttk.Button(move_box, text="↑", width=3)
        self.fine_up_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=(0, 4))
        self.down_btn = ttk.Button(move_box, text="Down")
        self.down_btn.grid(row=1, column=0, sticky="ew")
        self.fine_down_btn = ttk.Button(move_box, text="↓", width=3)
        self.fine_down_btn.grid(row=1, column=1, sticky="ew", padx=(4, 0))
        ttk.Button(move_box, text="Stop", command=self.stop_motion).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        range_box = ttk.Frame(speed_box)
        range_box.grid(row=2, column=0, sticky="ew", padx=4, pady=(0, 4))
        range_box.columnconfigure(1, weight=1)
        range_box.columnconfigure(3, weight=1)
        ttk.Label(range_box, text="Pos").grid(row=0, column=0, sticky="w")
        ttk.Label(range_box, textvariable=self.current_focus_var, width=11).grid(row=0, column=1, sticky="w")
        ttk.Button(range_box, text="Read", command=self.read_focus_once).grid(row=0, column=2, columnspan=2, sticky="ew", padx=(4, 0))
        ttk.Label(range_box, text="Top").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(range_box, text="0.000 mm", width=11).grid(row=1, column=1, sticky="w", pady=(4, 0))
        ttk.Button(range_box, text="Autofocus", command=self.auto_focus).grid(row=1, column=2, columnspan=2, sticky="ew", padx=(4, 0), pady=(4, 0))
        ttk.Label(range_box, text="Low").grid(row=2, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(range_box, textvariable=self.focus_low_limit_var, width=9).grid(row=2, column=1, sticky="ew", pady=(4, 0))
        ttk.Button(range_box, text="Here", command=self.capture_focus_low_limit).grid(row=2, column=2, sticky="ew", padx=(4, 2), pady=(4, 0))
        ttk.Label(range_box, text="Set").grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(range_box, textvariable=self.focus_set_position_var, width=9).grid(row=3, column=1, sticky="ew", pady=(4, 0))
        ttk.Button(range_box, text="Set", command=self.set_focus_position_manual).grid(row=3, column=2, sticky="ew", padx=(4, 2), pady=(4, 0))

        self.up_btn.bind("<ButtonPress-1>", lambda e: self.start_hold_move("up"))
        self.down_btn.bind("<ButtonPress-1>", lambda e: self.start_hold_move("down"))
        self.fine_up_btn.bind("<ButtonPress-1>", lambda e: self.start_hold_move("up", speed_override=self.FINE_JOG_SPEED, precise=True))
        self.fine_down_btn.bind("<ButtonPress-1>", lambda e: self.start_hold_move("down", speed_override=self.FINE_JOG_SPEED, precise=True))
        self.up_btn.bind("<ButtonRelease-1>", self._handle_global_button_release)
        self.down_btn.bind("<ButtonRelease-1>", self._handle_global_button_release)
        self.fine_up_btn.bind("<ButtonRelease-1>", self._handle_global_button_release)
        self.fine_down_btn.bind("<ButtonRelease-1>", self._handle_global_button_release)
        self.bind_all("<ButtonRelease-1>", self._handle_global_button_release, add="+")
        self.bind_all("<FocusOut>", self._handle_global_button_release, add="+")

        # Objective + Safe height combined
        obj = ttk.LabelFrame(focus, text="Objective")
        obj.grid(row=0, column=1, sticky="nsew", pady=(0, 3))
        obj.columnconfigure(1, weight=1)
        obj.columnconfigure(3, weight=1)

        ttk.Label(obj, text="Lens").grid(row=0, column=0, sticky="w", padx=4, pady=(4, 2))
        ttk.Label(obj, textvariable=self.current_lens_var, width=10).grid(row=0, column=1, sticky="w", pady=(4, 2))

        ttk.Label(obj, text="Lift mm").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        ttk.Entry(obj, textvariable=self.objective_lift_distance_mm_var, width=7).grid(row=1, column=1, sticky="w", padx=(0, 4), pady=2)
        ttk.Label(obj, text="Wait").grid(row=1, column=2, sticky="w", padx=(4, 0), pady=2)
        ttk.Entry(obj, textvariable=self.objective_change_timeout_var, width=7).grid(row=1, column=3, sticky="w", padx=(0, 4), pady=2)

        ttk.Label(obj, text="Safe mm").grid(row=2, column=0, sticky="w", padx=4, pady=2)
        ttk.Entry(obj, textvariable=self.safe_home_distance_mm_var, width=7).grid(row=2, column=1, sticky="w", padx=(0, 4), pady=2)
        ttk.Button(obj, text="Safe", command=self.move_home_safe).grid(row=2, column=2, columnspan=2, sticky="ew", padx=(4, 4), pady=2)

        ttk.Button(
            obj,
            text="Initialize 10x",
            command=self.initialize_default_objective,
        ).grid(row=3, column=0, columnspan=4, sticky="ew", padx=4, pady=2)

        btn_frame = ttk.Frame(obj)
        btn_frame.grid(row=4, column=0, columnspan=4, sticky="ew", padx=4, pady=(2, 4))
        for col in range(3):
            btn_frame.columnconfigure(col, weight=1)
        for lens in range(1, 6):
            text = NosepieceController.MAG_LABELS[lens]
            row = 0 if lens <= 3 else 1
            col = lens - 1 if lens <= 3 else lens - 4
            ttk.Button(
                btn_frame,
                text=text,
                command=lambda l=lens: self.change_objective(l),
                width=7,
            ).grid(row=row, column=col, padx=2, pady=2, sticky="ew")

        self.elev_port_combo = None
        self.obj_port_combo = None
        self.focus_connect_btn = None
        self.obj_connect_btn = None
        if self.show_connections:
            # Serial port connections for the focus elevator and objective nosepiece
            ports = ttk.Frame(self, padding=(0, 6, 0, 0))
            ports.grid(row=1, column=0, sticky="ew")
            ports.columnconfigure(1, weight=1)
            ports.columnconfigure(4, weight=1)

            ttk.Label(ports, text="Focus port:").grid(row=0, column=0, sticky="w", padx=(0, 2))
            self.elev_port_combo = ttk.Combobox(ports, textvariable=self.elev_port_var, width=8, state="readonly")
            self.elev_port_combo.grid(row=0, column=1, sticky="ew", padx=(0, 2))
            self.focus_connect_btn = ttk.Button(ports, text="Connect", command=self._connect_elevator_clicked)
            self.focus_connect_btn.grid(row=0, column=2, sticky="ew", padx=(0, 6))

            ttk.Label(ports, text="Obj port:").grid(row=0, column=3, sticky="w", padx=(0, 2))
            self.obj_port_combo = ttk.Combobox(ports, textvariable=self.obj_port_var, width=8, state="readonly")
            self.obj_port_combo.grid(row=0, column=4, sticky="ew", padx=(0, 2))
            self.obj_connect_btn = ttk.Button(ports, text="Connect", command=self._connect_nosepiece_clicked)
            self.obj_connect_btn.grid(row=0, column=5, sticky="ew", padx=(0, 6))

            ttk.Button(ports, text="Refresh", command=self.refresh_ports).grid(row=0, column=6, sticky="ew", padx=(0, 2))
            ttk.Button(ports, text="Disconnect", style="Danger.TButton", command=self._disconnect_all_clicked).grid(row=0, column=7, sticky="ew")

            self.refresh_ports()

    # ───────────────────────── Helpers ─────────────────────────
    def log(self, msg: str):
        if getattr(self, "external_log", None):
            try:
                self.external_log(f"[MICRO] {msg}")
                return
            except Exception:
                pass
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {msg}")

    def _show_objective_initialization_popup(self):
        """Show a non-modal progress window for the 10x initialization."""
        try:
            existing = self._objective_init_popup
            if existing is not None and existing.winfo_exists():
                existing.deiconify()
                existing.lift()
                return
        except Exception:
            self._objective_init_popup = None

        try:
            popup = tk.Toplevel(self)
            popup.title("Objective Initialization")
            popup.transient(self.winfo_toplevel())
            popup.resizable(False, False)
            popup.protocol("WM_DELETE_WINDOW", lambda: None)

            body = ttk.Frame(popup, padding=16)
            body.grid(row=0, column=0, sticky="nsew")
            ttk.Label(
                body,
                text="Initializing the 10x objective",
                font=("TkDefaultFont", 10, "bold"),
            ).grid(row=0, column=0, sticky="w")

            status_var = tk.StringVar(value="Reading the focus position...")
            ttk.Label(body, textvariable=status_var, width=38).grid(
                row=1,
                column=0,
                sticky="w",
                pady=(8, 8),
            )
            progress = ttk.Progressbar(body, mode="indeterminate", length=280)
            progress.grid(row=2, column=0, sticky="ew")
            progress.start(12)

            popup.update_idletasks()
            try:
                x = self.winfo_rootx() + max(0, (self.winfo_width() - popup.winfo_width()) // 2)
                y = self.winfo_rooty() + max(0, (self.winfo_height() - popup.winfo_height()) // 2)
                popup.geometry(f"+{x}+{y}")
            except Exception:
                pass
            popup.lift()

            self._objective_init_popup = popup
            self._objective_init_progress = progress
            self._objective_init_status_var = status_var
        except Exception:
            self._objective_init_popup = None
            self._objective_init_progress = None
            self._objective_init_status_var = None

    def _set_objective_initialization_status(self, text: str):
        status_var = getattr(self, "_objective_init_status_var", None)
        if status_var is not None:
            self._schedule_ui_var_set("objective_init_status", status_var, text)

    def _destroy_objective_initialization_popup(self):
        progress = getattr(self, "_objective_init_progress", None)
        popup = getattr(self, "_objective_init_popup", None)
        self._objective_init_progress = None
        self._objective_init_popup = None
        self._objective_init_status_var = None
        try:
            if progress is not None:
                progress.stop()
        except Exception:
            pass
        try:
            if popup is not None and popup.winfo_exists():
                popup.destroy()
        except Exception:
            pass

    def _close_objective_initialization_popup(self):
        try:
            self.after(0, self._destroy_objective_initialization_popup)
        except Exception:
            self._destroy_objective_initialization_popup()

    def _schedule_ui_var_set(self, key: str, var: tk.Variable, value: str):
        if self._closing or not self.winfo_exists():
            return

        aid = self._ui_after_ids.pop(key, None)
        if aid:
            try:
                self.after_cancel(aid)
            except Exception:
                pass

        def _apply():
            self._ui_after_ids.pop(key, None)
            if self._closing or not self.winfo_exists():
                return
            try:
                var.set(value)
            except Exception:
                pass

        try:
            self._ui_after_ids[key] = self.after(0, _apply)
        except Exception:
            pass

    def _set_focus_position_var(self, pos: Optional[float]):
        text = "-- mm" if pos is None else f"{pos:.3f} mm"
        self._schedule_ui_var_set("current_focus", self.current_focus_var, text)

    def _set_focus_low_limit_var(self, value: float):
        self.focus_low_limit_mm = value
        formatted = f"{value:.3f}"
        self._schedule_ui_var_set("focus_low_limit", self.focus_low_limit_var, formatted)

    def _on_focus_low_limit_var_changed(self, *_args):
        raw_low = (self.focus_low_limit_var.get() or "").strip()
        if not raw_low:
            self.focus_low_limit_mm = None
            return
        try:
            low_mm = float(raw_low)
        except ValueError:
            return
        if 0 < low_mm <= ElevatorController.MAX_POSITION_MM:
            self.focus_low_limit_mm = low_mm

    def _set_current_lens_var(self, lens: Optional[int]):
        if lens is None:
            text = "--"
        else:
            label = NosepieceController.MAG_LABELS.get(lens, str(lens))
            text = f"{lens} ({label})"
        if text == self._last_lens_display:
            return
        self._last_lens_display = text
        self._schedule_ui_var_set("current_lens", self.current_lens_var, text)

    def _get_focus_lower_limit(self) -> Optional[float]:
        raw_low = (self.focus_low_limit_var.get() or "").strip()
        if not raw_low:
            return self.focus_low_limit_mm
        low_mm = float(raw_low)
        if low_mm <= 0:
            raise ValueError("Lowest focus position must be greater than 0 mm.")
        if low_mm > ElevatorController.MAX_POSITION_MM:
            raise ValueError(
                f"Lowest focus position must not exceed "
                f"{ElevatorController.MAX_POSITION_MM:.3f} mm."
            )
        self.focus_low_limit_mm = low_mm
        return low_mm

    @classmethod
    def _objective_move_distance_mm(cls, base_distance_mm: float, lens: int) -> float:
        if lens not in cls.OBJECTIVE_FOCUS_OFFSETS_MM:
            raise ValueError(f"No focus offset is configured for objective position {lens}.")
        distance_mm = base_distance_mm + cls.OBJECTIVE_FOCUS_OFFSETS_MM[lens]
        if distance_mm <= 0:
            label = NosepieceController.MAG_LABELS.get(lens, str(lens))
            raise ValueError(
                f"Adjusted movement for {label} must be > 0 mm "
                f"(base {base_distance_mm:.3f} mm)."
            )
        return distance_mm

    @staticmethod
    def _estimate_focus_distance_mm(speed: int, duration: float) -> float:
        return (speed * 10) * duration / 1000.0

    @staticmethod
    def _duration_for_distance_mm(speed: int, distance_mm: float) -> float:
        if speed <= 0 or distance_mm <= 0:
            return 0.0
        return (distance_mm * 1000.0) / (speed * 10)

    def _run_focus_move_distance(self, direction: str, distance_mm: float, speed: int,
                                 lower_limit: Optional[float] = None,
                                 allow_noop_at_limit: bool = False,
                                 stop_event: Optional[threading.Event] = None,
                                 precise: bool = True) -> bool:
        duration = self._duration_for_distance_mm(speed, distance_mm)
        if duration <= 0:
            return False
        return self._run_focus_move(
            direction,
            duration,
            speed,
            lower_limit=lower_limit,
            allow_noop_at_limit=allow_noop_at_limit,
            stop_event=stop_event,
            precise=precise,
        )

    def _run_fine_jog_step(self, direction: str, speed: int,
                           lower_limit: Optional[float],
                           stop_event: threading.Event) -> bool:
        """Move one verified, discrete fine-jog increment from the last actual Z."""
        if not self.elevator or not self.elevator.is_connected:
            return False
        if stop_event.is_set():
            return False

        current_position = self.elevator.current_position
        if current_position is None:
            current_position = self.elevator.get_current_position()
        if current_position is None:
            self.log("Fine jog cancelled: current position could not be read.")
            return False

        target_position = (
            current_position - self.FINE_JOG_STEP_MM
            if direction == "up"
            else current_position + self.FINE_JOG_STEP_MM
        )
        fine_lower_limit = min(
            lower_limit
            if lower_limit is not None
            else ElevatorController.MAX_POSITION_MM,
            ElevatorController.MAX_POSITION_MM,
        )
        target_position = max(target_position, ElevatorController.MIN_POSITION_MM)
        target_position = min(target_position, fine_lower_limit)

        if abs(target_position - current_position) < 0.001:
            limit = ElevatorController.MIN_POSITION_MM if direction == "up" else fine_lower_limit
            self.log(f"Fine {direction} jog blocked at limit {limit:.3f} mm.")
            return False

        ok = self.elevator.move_to_position(
            target_position,
            speed=speed,
            current_pos=current_position,
            cancel_event=stop_event,
            arrival_tolerance_mm=self.FINE_JOG_ARRIVAL_TOLERANCE_MM,
            max_attempts=1,
            accept_settled_offset=True,
        )
        self._set_focus_position_var(self.elevator.current_position)
        if ok and self.elevator.current_position is not None:
            self.log(
                f"Fine {direction} jog: target {target_position:.3f} mm, "
                f"measured {self.elevator.current_position:.3f} mm."
            )
        elif not stop_event.is_set():
            detail = self.elevator.last_position_error or "unknown elevator error"
            self.log(f"Fine {direction} jog failed: {detail}.")
        return ok

    def _run_focus_move(self, direction: str, duration: float, speed: int,
                        lower_limit: Optional[float] = None,
                        allow_noop_at_limit: bool = False,
                        stop_event: Optional[threading.Event] = None,
                        precise: bool = False) -> bool:
        if not self.elevator or not self.elevator.is_connected:
            return False
        if duration <= 0:
            return False

        current_pos = self.elevator.current_position
        if current_pos is None:
            current_pos = self.elevator.get_current_position()
        self._set_focus_position_var(current_pos)
        if current_pos is None:
            self.log("Focus position unavailable; cannot enforce working range.")
            return False

        epsilon_mm = 0.001
        step_mm = self._estimate_focus_distance_mm(speed, duration)
        target_pos = current_pos - step_mm if direction == "up" else current_pos + step_mm
        bounded_target = max(target_pos, ElevatorController.MIN_POSITION_MM)
        bounded_target = min(bounded_target, ElevatorController.MAX_POSITION_MM)
        if lower_limit is not None:
            bounded_target = min(bounded_target, lower_limit)

        if abs(bounded_target - current_pos) < epsilon_mm:
            if direction == "up":
                self.log("Focus blocked at top working limit (0.000 mm).")
            else:
                self.log(f"Focus blocked at lowest working limit ({current_pos:.3f} mm).")
            return allow_noop_at_limit

        if precise:
            if abs(bounded_target - target_pos) >= epsilon_mm:
                self.log(f"Clamped fine focus move to {bounded_target:.3f} mm.")
            ok = self.elevator.move_to_position(
                bounded_target,
                speed=speed,
                current_pos=current_pos,
                cancel_event=stop_event,
            )
            self._set_focus_position_var(self.elevator.current_position)
            return ok

        pulse_s = 0.05
        remaining = duration
        moved_any = False

        try:
            while remaining > 0:
                if stop_event is not None and stop_event.is_set():
                    return moved_any or allow_noop_at_limit

                step_s = min(pulse_s, remaining)
                step_mm = self._estimate_focus_distance_mm(speed, step_s)

                if direction == "up":
                    allowed_mm = max(0.0, current_pos - ElevatorController.MIN_POSITION_MM)
                    if allowed_mm <= epsilon_mm:
                        self.log("Focus blocked at top working limit (0.000 mm).")
                        return allow_noop_at_limit or moved_any
                else:
                    effective_lower_limit = ElevatorController.MAX_POSITION_MM
                    if lower_limit is not None:
                        effective_lower_limit = min(effective_lower_limit, lower_limit)
                    allowed_mm = max(0.0, effective_lower_limit - current_pos)
                    if allowed_mm <= epsilon_mm:
                        self.log(f"Focus blocked at lowest working limit ({current_pos:.3f} mm).")
                        return allow_noop_at_limit or moved_any

                if step_mm > allowed_mm:
                    step_mm = allowed_mm
                    step_s = step_mm * 1000.0 / (speed * 10) if speed > 0 else 0.0
                    if step_s <= 0:
                        return allow_noop_at_limit or moved_any
                    if direction == "up":
                        self.log("Clamped focus move to top working limit at 0.000 mm.")
                    else:
                        self.log(f"Clamped focus move to lowest working limit at {current_pos + step_mm:.3f} mm.")

                ok = self.elevator.move_up(speed) if direction == "up" else self.elevator.move_down(speed)
                if not ok:
                    return False

                deadline = time.time() + step_s
                while time.time() < deadline:
                    if stop_event is not None and stop_event.is_set():
                        return moved_any or allow_noop_at_limit
                    time.sleep(min(0.01, max(0.0, deadline - time.time())))

                remaining -= step_s
                moved_any = True

                if direction == "up":
                    current_pos = max(0.0, current_pos - step_mm)
                else:
                    current_pos += step_mm

                self.elevator.current_position = current_pos
                self._set_focus_position_var(current_pos)

            return True
        finally:
            self.elevator.stop()
            self._set_focus_position_var(self.elevator.current_position)

    def _load_autofocus_lens_config(self) -> dict:
        try:
            with open(AUTOFOCUS_LENS_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            self.log(f"Autofocus: failed to read lens config: {e}")
            return {}

    def _get_autofocus_params_for_lens(self, lens: Optional[int]) -> tuple[float, float, float, float]:
        """Look up the start height, good-focus score, and step sizes calibrated for
        `lens`. Depth of field shrinks at higher magnification, so a step size tuned
        for one lens can step right over the (narrower) focus peak on another -
        step sizes are per-lens for that reason.

        Falls back to the class defaults (and logs why) when the nosepiece's current
        lens is unknown or has no calibration entry in the JSON config yet.
        """
        entry = self._load_autofocus_lens_config().get(str(lens)) if lens is not None else None
        if entry is None:
            self.log(
                f"Autofocus: no calibration for lens {lens}; falling back to defaults "
                f"(start={self.AUTOFOCUS_START_HEIGHT_MM:.3f} mm, "
                f"good_score={self.AUTOFOCUS_BRENNER_GOOD_SCORE:.1f}, "
                f"brenner_step={self.AUTOFOCUS_BRENNER_STEP_MM:.4f} mm, "
                f"laplacian_step={self.AUTOFOCUS_LAPLACIAN_STEP_MM:.4f} mm)."
            )
            return (self.AUTOFOCUS_START_HEIGHT_MM, self.AUTOFOCUS_BRENNER_GOOD_SCORE,
                    self.AUTOFOCUS_BRENNER_STEP_MM, self.AUTOFOCUS_LAPLACIAN_STEP_MM)

        start_height_mm = float(entry.get("start_height_mm", self.AUTOFOCUS_START_HEIGHT_MM))
        good_score = float(entry.get("good_score", self.AUTOFOCUS_BRENNER_GOOD_SCORE))
        brenner_step_mm = float(entry.get("brenner_step_mm", self.AUTOFOCUS_BRENNER_STEP_MM))
        laplacian_step_mm = float(entry.get("laplacian_step_mm", self.AUTOFOCUS_LAPLACIAN_STEP_MM))
        label = entry.get("label", str(lens))
        self.log(
            f"Autofocus: using lens {lens} ({label}) calibration: "
            f"start={start_height_mm:.3f} mm, good_score={good_score:.1f}, "
            f"brenner_step={brenner_step_mm:.4f} mm, laplacian_step={laplacian_step_mm:.4f} mm."
        )
        return start_height_mm, good_score, brenner_step_mm, laplacian_step_mm

    def auto_focus(self):
        if not self._require_elevator():
            return
        if getattr(self, "external_snap", None) is None:
            messagebox.showwarning("Autofocus", "Live camera snapshot is not available.")
            return
        try:
            focus_lower_limit = self._get_focus_lower_limit()
        except ValueError as exc:
            messagebox.showwarning("Focus", str(exc))
            return

        lens = self.nosepiece.current_lens if (self.nosepiece and self.nosepiece.is_connected) else None
        start_height_mm, good_score, brenner_step_mm, laplacian_step_mm = (
            self._get_autofocus_params_for_lens(lens)
        )

        if not self._prepare_new_motion():
            return
        stop_event = self.move_stop_event

        def worker():
            af = self.focus_calculator
            try:
                with self.busy_lock:
                    start_pos = self._clamp_autofocus_target(start_height_mm, focus_lower_limit)
                    self.log(f"Autofocus: moving to start height {start_pos:.3f} mm.")
                    if not self._move_focus_absolute(start_pos, stop_event,
                                                      speed=self.MEDIUM_ELEVATOR_SPEED):
                        self.log("Autofocus: failed to reach start height; aborting.")
                        return

                    self.log("Autofocus: coarse pass (Brenner gradient).")
                    self._run_brenner_descent_phase(
                        af, brenner_step_mm,
                        af.get_absolute_focus_score_brenner,
                        focus_lower_limit, stop_event, good_score,
                    )
                    if stop_event.is_set():
                        self.log("Autofocus cancelled.")
                        return

                    self.log("Autofocus: fine pass (Laplacian).")
                    final_pos = self._run_laplacian_descent_phase(
                        af, laplacian_step_mm,
                        af.get_absolute_focus_score_laplacian,
                        focus_lower_limit, stop_event,
                    )
                    if stop_event.is_set():
                        self.log("Autofocus cancelled.")
                    else:
                        self.log(f"Autofocus complete at {final_pos:.3f} mm.")
            except Exception as e:
                self.log(f"Autofocus failed: {e}")
            finally:
                self._clear_move_thread_if_current()

        self.move_thread = threading.Thread(target=worker, daemon=True)
        self.move_thread.start()

    def _run_brenner_descent_phase(self, af, step_mm: float, get_absolute_score,
                                   lower_limit: Optional[float],
                                   stop_event: threading.Event, good_score: float) -> float:
        """Step focus downward while sharpness keeps improving. If the first downward
        step doesn't help, try upward instead before settling at the start height."""
        af.set_step_size(step_mm)
        return self._run_directional_focus_phase(
            "Brenner", step_mm, get_absolute_score, lower_limit, stop_event,
            self.AUTOFOCUS_BRENNER_PATIENCE, good_score=good_score,
            relative_gain=self.AUTOFOCUS_BRENNER_RELATIVE_GAIN,
        )

    def _run_laplacian_descent_phase(self, af, step_mm: float, get_absolute_score,
                                     lower_limit: Optional[float],
                                     stop_event: threading.Event) -> float:
        """Step focus downward while sharpness keeps improving. If the first downward
        step doesn't help, try upward instead before settling at the start position."""
        af.set_step_size(step_mm)
        return self._run_directional_focus_phase(
            "Laplacian", step_mm, get_absolute_score, lower_limit, stop_event,
            self.AUTOFOCUS_LAPLACIAN_PATIENCE,
        )

    def _run_directional_focus_phase(self, label: str, step_mm: float, get_absolute_score,
                                     lower_limit: Optional[float], stop_event: threading.Event,
                                     patience: int, good_score: Optional[float] = None,
                                     relative_gain: Optional[float] = None) -> float:
        """Try stepping focus down, then (if that doesn't help) up, tracking the best
        position found in either direction."""
        start_pos = self.elevator.current_position
        if start_pos is None:
            start_pos = self.elevator.get_current_position()
        if start_pos is None:
            raise RuntimeError("Focus position unavailable; cannot autofocus.")

        best_pos = start_pos
        best_score = get_absolute_score(self._snap_autofocus_frame())
        self.log(f"Autofocus [{label}] start: pos={best_pos:.3f} score={best_score:.1f}")

        best_pos, best_score, improved = self._directional_focus_search(
            start_pos, +1, f"{label} down", step_mm, get_absolute_score,
            lower_limit, stop_event, best_pos, best_score, patience,
            good_score=good_score, relative_gain=relative_gain,
        )
        if not improved and not stop_event.is_set():
            self.log(f"Autofocus [{label}]: downward step didn't help, trying upward.")
            best_pos, best_score, _ = self._directional_focus_search(
                start_pos, -1, f"{label} up", step_mm, get_absolute_score,
                lower_limit, stop_event, best_pos, best_score, patience,
                good_score=good_score, relative_gain=relative_gain,
            )

        return best_pos

    def _directional_focus_search(self, current_pos: float, direction: int, label: str,
                                   step_mm: float, get_absolute_score, lower_limit: Optional[float],
                                   stop_event: threading.Event, best_pos: float, best_score: float,
                                   patience: int, good_score: Optional[float] = None,
                                   relative_gain: Optional[float] = None) -> tuple[float, float, bool]:
        """Step focus in `direction` (+1 = down/increase mm, -1 = up/decrease mm), tracking
        the best (most recent, on ties) position seen. Tolerates up to `patience` consecutive
        non-improving steps (the score curve can dip from noise) before giving up and
        reverting to the best position found over the whole excursion. If `good_score` /
        `relative_gain` are given, also stops immediately once the score clears that
        absolute or relative-to-start threshold, rather than waiting to see if it keeps
        climbing."""
        starting_best_score = best_score
        stale_steps = 0
        for iteration in range(1, self.AUTOFOCUS_MAX_ITERATIONS + 1):
            if stop_event.is_set():
                break

            next_pos = self._clamp_autofocus_target(current_pos + direction * step_mm, lower_limit)
            if next_pos == current_pos:
                limit_desc = "lowest" if direction > 0 else "top"
                self.log(f"Autofocus [{label}]: reached {limit_desc} focus limit.")
                break

            if not self._move_focus_absolute(next_pos, stop_event):
                break
            score = get_absolute_score(self._snap_autofocus_frame())
            self.log(f"Autofocus [{label}] iter {iteration}: pos={next_pos:.3f} score={score:.1f}")

            current_pos = next_pos
            if score >= best_score:
                best_score = score
                best_pos = next_pos
                stale_steps = 0
                if good_score is not None and best_score >= good_score:
                    self.log(
                        f"Autofocus [{label}]: score {best_score:.1f} clears the "
                        f"good-focus threshold ({good_score:.1f}); stopping early."
                    )
                    break
                if relative_gain is not None and starting_best_score > 0:
                    gain = best_score / starting_best_score - 1.0
                    if gain >= relative_gain:
                        self.log(
                            f"Autofocus [{label}]: score {best_score:.1f} is "
                            f"{gain:.0%} above the starting score "
                            f"({starting_best_score:.1f}); stopping early."
                        )
                        break
            else:
                stale_steps += 1
                if stale_steps >= patience:
                    self.log(f"Autofocus [{label}]: no improvement; reverting to best.")
                    break

        self._move_focus_absolute(best_pos, stop_event)
        improved = best_score > starting_best_score
        return best_pos, best_score, improved

    def _snap_autofocus_frame(self):
        snap = getattr(self, "external_snap", None)
        if snap is None:
            raise RuntimeError("Live camera snapshot is not available.")
        frame = snap()
        if frame is None:
            raise RuntimeError("No camera frame available for autofocus.")
        return frame

    def _move_focus_absolute(self, target_mm: float, stop_event: threading.Event,
                             speed: Optional[int] = None) -> bool:
        if stop_event.is_set():
            return False
        ok = self.elevator.move_to_position(target_mm, speed=speed or self.FINE_ELEVATOR_SPEED,
                                            cancel_event=stop_event)
        if ok:
            self.elevator.wait_for_motion_idle()
            time.sleep(self.AUTOFOCUS_SETTLE_DELAY_S)
        self._set_focus_position_var(self.elevator.current_position)
        return ok

    @staticmethod
    def _clamp_autofocus_target(target_mm: float, lower_limit: Optional[float]) -> float:
        clamped = max(target_mm, ElevatorController.MIN_POSITION_MM)
        clamped = min(clamped, ElevatorController.MAX_POSITION_MM)
        if lower_limit is not None:
            clamped = min(clamped, lower_limit)
        return clamped


    def read_focus_once(self):
        if not self._require_elevator():
            return

        def worker():
            pos = self.elevator.get_current_position()
            self._set_focus_position_var(pos)
            if pos is None:
                detail = self.elevator.last_position_error or "no response"
                self.log(f"Focus position read failed: {detail}")
            else:
                self.log(f"Current focus position: {pos:.3f} mm")

        threading.Thread(target=worker, daemon=True).start()

    def set_focus_position_manual(self):
        """Seed the soft-tracked elevator position by hand.

        Workaround for controllers whose position-query reply the app can't
        parse: jog to a known reference (a hard stop, or a height measured
        some other way), type it here, and moves/autofocus/scan corner
        capture will trust this value instead of re-querying the hardware.
        """
        if not self._require_elevator():
            return
        raw = (self.focus_set_position_var.get() or "").strip()
        try:
            value = float(raw)
        except ValueError:
            messagebox.showwarning("Focus", "Enter a numeric position in mm.")
            return
        if not ElevatorController.MIN_POSITION_MM <= value <= ElevatorController.MAX_POSITION_MM:
            messagebox.showwarning(
                "Focus",
                f"Position must be between {ElevatorController.MIN_POSITION_MM:.3f} and "
                f"{ElevatorController.MAX_POSITION_MM:.3f} mm.",
            )
            return
        self.elevator.current_position = value
        self._set_focus_position_var(value)
        self.log(f"Focus position manually set to {value:.3f} mm.")

    def capture_focus_low_limit(self):
        if not self._require_elevator():
            return

        def worker():
            pos = self.elevator.current_position
            if pos is None:
                pos = self.elevator.get_current_position()
            self._set_focus_position_var(pos)
            if pos is None:
                self.log("Could not read focus position to capture lowest focus limit.")
                return
            if pos <= 0:
                self.log("Lowest focus limit must be below the top position; move down before capturing.")
                return
            if pos > ElevatorController.MAX_POSITION_MM:
                self.log(
                    f"Cannot capture a lowest focus limit above "
                    f"{ElevatorController.MAX_POSITION_MM:.3f} mm."
                )
                return
            self._set_focus_low_limit_var(pos)
            self.log(f"Set lowest focus limit to {pos:.3f} mm.")

        threading.Thread(target=worker, daemon=True).start()

    def refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        if self.elev_port_combo is not None:
            self.elev_port_combo["values"] = ports
        if self.obj_port_combo is not None:
            self.obj_port_combo["values"] = ports
        if ports and not self.elev_port_var.get():
            self.elev_port_var.set(ports[0])
        if len(ports) > 1 and not self.obj_port_var.get():
            self.obj_port_var.set(ports[1])
        elif ports and not self.obj_port_var.get():
            self.obj_port_var.set(ports[0])
        self.log(f"Ports: {ports}")

    def connect_elevator(self, show_error: bool = True, port: Optional[str] = None,
                         read_initial_position: bool = True):
        if port is None:
            port = self.elev_port_var.get()
        port = (port or "").strip()
        if not port:
            if show_error:
                messagebox.showwarning("Focus", "Select a focus/elevator serial port first.")
            return False
        if self.elevator and self.elevator.is_connected and self.elevator.port == port:
            self.log(f"Focus already connected on {port}.")
            return True
        ctrl = ElevatorController(
            port=port,
            baudrate=FOCUS_BAUDRATE,
            protocol_logger=self.log if self.LOG_ELEVATOR_PROTOCOL else None,
        )
        if ctrl.connect():
            self.elevator = ctrl
            self.log(f"Focus connected on {port}.")
            if read_initial_position:
                self.read_focus_once()
            return True
        else:
            if show_error:
                messagebox.showerror("Focus", f"Could not connect on {port}.")
            self.log(f"Focus connect failed on {port}.")
            return False

    def connect_nosepiece(self, show_error: bool = True, initialize_default: bool = False,
                          port: Optional[str] = None, ui_dispatch=None):
        if port is None:
            port = self.obj_port_var.get()
        port = (port or "").strip()
        if not port:
            if show_error:
                messagebox.showwarning("Objective", "Select an objective serial port first.")
            return False
        if self.nosepiece and self.nosepiece.is_connected and self.nosepiece.port == port:
            self.log(f"Objective already connected on {port}.")
            if initialize_default:
                if ui_dispatch is None:
                    self.after(0, self.initialize_default_objective)
                else:
                    ui_dispatch(self.initialize_default_objective)
            else:
                self.read_objective_once(wait_timeout_s=self.OBJECTIVE_CONNECT_READ_TIMEOUT_S)
            return True
        ctrl = NosepieceController(port=port, baudrate=OBJECTIVE_BAUDRATE)
        if ctrl.connect():
            self.nosepiece = ctrl
            self.log(f"Objective connected on {port}.")
            if initialize_default:
                if ui_dispatch is None:
                    self.after(0, self.initialize_default_objective)
                else:
                    ui_dispatch(self.initialize_default_objective)
            else:
                self.read_objective_once(wait_timeout_s=self.OBJECTIVE_CONNECT_READ_TIMEOUT_S)
            return True
        else:
            if show_error:
                messagebox.showerror("Objective", f"Could not connect on {port}.")
            self.log(f"Objective connect failed on {port}.")
            return False

    def _connect_elevator_clicked(self):
        if self.connect_elevator(show_error=True):
            self._mark_port_connected(self.focus_connect_btn)

    def _connect_nosepiece_clicked(self):
        if self.connect_nosepiece(show_error=True):
            self._mark_port_connected(self.obj_connect_btn)

    def _mark_port_connected(self, btn):
        text = getattr(self, "connect_button_connected_text", "Connected")
        try:
            btn.config(text=text)
        except Exception:
            pass
        try:
            btn.config(style="Success.TButton")
        except Exception:
            try:
                btn.config(bg=getattr(self, "connect_button_connected_bg", "#1A7A3F"))
            except Exception:
                pass

    def _disconnect_all_clicked(self):
        self.disconnect_all()
        text = getattr(self, "connect_button_idle_text", "Connect")
        for btn in (self.focus_connect_btn, self.obj_connect_btn):
            try:
                btn.config(text=text)
            except Exception:
                pass
            try:
                btn.config(style="TButton")
            except Exception:
                try:
                    btn.config(bg=getattr(self, "connect_button_idle_bg", "#2471B5"))
                except Exception:
                    pass

    def disconnect_all(self):
        self.stop_motion(log_action=False)
        try:
            if self.elevator:
                self.elevator.disconnect()
            if self.nosepiece:
                self.nosepiece.disconnect()
        finally:
            self._set_focus_position_var(None)
            self._set_current_lens_var(None)
            self.log("Disconnected focus/objective.")

    def _require_elevator(self) -> bool:
        if self.elevator and self.elevator.is_connected:
            return True
        messagebox.showwarning("Focus", "Focus controller is not connected.")
        return False

    def _require_nosepiece(self) -> bool:
        if self.nosepiece and self.nosepiece.is_connected:
            return True
        messagebox.showwarning("Objective", "Objective controller is not connected.")
        return False

    # ───────────────────────── Motion ─────────────────────────
    def start_hold_move(self, direction: str, speed_override: Optional[int] = None,
                        precise: bool = False):
        if not self._require_elevator():
            return
        try:
            focus_lower_limit = self._get_focus_lower_limit()
        except ValueError as exc:
            messagebox.showwarning("Focus", str(exc))
            return
        if not self._prepare_new_motion():
            return
        self.active_hold_direction = direction
        self._normal_jog_active = not precise
        speed = speed_override if speed_override is not None else self.speed_var.get()

        def runner():
            try:
                mode = "fine" if precise else "normal"
                if precise:
                    current_position = self.elevator.get_current_position()
                    self._set_focus_position_var(current_position)
                    if current_position is None:
                        if self.active_hold_direction == direction:
                            self.active_hold_direction = None
                        detail = self.elevator.last_position_error or "no response"
                        self.log(
                            "Fine focus cancelled: current position could not be read "
                            f"({detail})."
                        )
                        return
                    self.log(f"Fine focus position updated: {current_position:.3f} mm.")
                else:
                    # Normal jog uses a conservative time estimate only for
                    # its guard limits. The UI is updated solely by a delayed
                    # encoder read after the elevator has been idle.
                    current_position = self.elevator.current_position
                    if current_position is None:
                        current_position = self.elevator.get_current_position()
                    if current_position is None:
                        self.log("Normal focus jog cancelled: current position could not be read.")
                        return
                self.log(f"Hold move {direction} at speed {speed} ({mode}).")
                if precise:
                    while not self.move_stop_event.is_set():
                        ok = self._run_fine_jog_step(
                            direction,
                            speed,
                            lower_limit=focus_lower_limit,
                            stop_event=self.move_stop_event,
                        )
                        if not ok:
                            self._set_focus_position_var(
                                self.elevator.current_position if self.elevator else None
                            )
                            break
                else:
                    normal_jog_limit = min(
                        focus_lower_limit
                        if focus_lower_limit is not None
                        else ElevatorController.MAX_POSITION_MM,
                        ElevatorController.MAX_POSITION_MM,
                    )
                    estimated_step_mm = self._estimate_focus_distance_mm(
                        speed,
                        self.NORMAL_JOG_COMMAND_INTERVAL_S,
                    )
                    while not self.move_stop_event.is_set():
                        at_upper_guard = (
                            direction == "up"
                            and current_position <= (
                                ElevatorController.MIN_POSITION_MM
                                + self.NORMAL_JOG_LIMIT_GUARD_MM
                            )
                        )
                        at_lower_guard = (
                            direction == "down"
                            and current_position >= (
                                normal_jog_limit - self.NORMAL_JOG_LIMIT_GUARD_MM
                            )
                        )
                        if at_upper_guard or at_lower_guard:
                            limit = (
                                ElevatorController.MIN_POSITION_MM
                                if direction == "up"
                                else normal_jog_limit
                            )
                            self.log(
                                f"Normal {direction} jog stopped at the {limit:.3f} mm "
                                f"limit guard."
                            )
                            break

                        ok = (
                            self.elevator.move_up(speed)
                            if direction == "up"
                            else self.elevator.move_down(speed)
                        )
                        if not ok:
                            self.log(f"Normal {direction} jog command failed.")
                            break
                        time.sleep(self.NORMAL_JOG_COMMAND_INTERVAL_S)
                        if direction == "up":
                            current_position = max(
                                ElevatorController.MIN_POSITION_MM,
                                current_position - estimated_step_mm,
                            )
                        else:
                            current_position = min(normal_jog_limit, current_position + estimated_step_mm)
                        self.elevator.current_position = current_position
                owns_hold = self.active_hold_direction == direction
                if self.elevator and self.elevator.is_connected:
                    # A button release has already sent the physical Stop and
                    # cleared active_hold_direction.  Only send another Stop
                    # when this worker stopped itself (for example, at a
                    # guard limit), but always schedule the normal-jog
                    # encoder read after the movement becomes idle.
                    if owns_hold:
                        self.elevator.stop()
                    if precise and owns_hold:
                        self._set_focus_position_var(self.elevator.current_position)
                    elif not precise:
                        self._schedule_normal_jog_position_read()
                        self.log(
                            f"Normal {direction} jog stopped; reading the encoder after "
                            f"{self.NORMAL_JOG_IDLE_READ_DELAY_S:.0f} s of inactivity."
                        )
                if owns_hold:
                    self.active_hold_direction = None
                self.log(f"Stopped {direction}.")
            finally:
                if not precise:
                    self._normal_jog_active = False
                self._clear_move_thread_if_current()

        self.move_thread = threading.Thread(target=runner, daemon=True)
        self.move_thread.start()

    def _handle_global_button_release(self, _event=None):
        if self.active_hold_direction is not None:
            self.stop_motion()

    def _clear_move_thread_if_current(self):
        if self.move_thread is threading.current_thread():
            self.move_thread = None

    def _cancel_normal_jog_position_read(self):
        with self._normal_jog_read_lock:
            self._normal_jog_read_generation += 1
            timer = self._normal_jog_read_timer
            self._normal_jog_read_timer = None
        if timer is not None:
            timer.cancel()

    def _schedule_normal_jog_position_read(self):
        self._cancel_normal_jog_position_read()
        with self._normal_jog_read_lock:
            generation = self._normal_jog_read_generation
            timer = threading.Timer(
                self.NORMAL_JOG_IDLE_READ_DELAY_S,
                self._read_normal_jog_position_after_idle,
                args=(generation,),
            )
            timer.daemon = True
            self._normal_jog_read_timer = timer
        timer.start()

    def _read_normal_jog_position_after_idle(self, generation: int):
        with self._normal_jog_read_lock:
            if generation != self._normal_jog_read_generation:
                return
            self._normal_jog_read_timer = None
        if self._closing or self._normal_jog_active:
            return
        active_thread = self.move_thread
        if active_thread and active_thread.is_alive():
            return
        if not (self.elevator and self.elevator.is_connected):
            return

        position = self.elevator.get_current_position()
        with self._normal_jog_read_lock:
            if generation != self._normal_jog_read_generation:
                return
        self._set_focus_position_var(position)
        if position is None:
            detail = self.elevator.last_position_error or "no response"
            self.log(f"Delayed normal-jog position read failed: {detail}.")
        else:
            self.log(f"Normal jog position after idle: {position:.3f} mm.")

    def _prepare_new_motion(self) -> bool:
        active_thread = self.move_thread
        elevator_moving = bool(
            self.elevator
            and self.elevator.is_connected
            and self.elevator.is_motion_active()
        )
        if (active_thread and active_thread.is_alive()) or elevator_moving:
            self.stop_motion(log_action=False)
            active_thread = self.move_thread
        if active_thread and active_thread.is_alive():
            self.log("Previous focus operation is still stopping; new movement was not started.")
            return False
        self._cancel_normal_jog_position_read()
        self.move_stop_event = threading.Event()
        return True

    def stop_motion(self, log_action: bool = True):
        self.move_stop_event.set()
        self.active_hold_direction = None
        if self.elevator and self.elevator.is_connected:
            self.elevator.stop()
            if not self._normal_jog_active:
                self._set_focus_position_var(self.elevator.current_position)
        active_thread = self.move_thread
        if active_thread and active_thread.is_alive() and active_thread is not threading.current_thread():
            active_thread.join(timeout=0.15)
        if active_thread and not active_thread.is_alive() and self.move_thread is active_thread:
            self.move_thread = None
        if log_action:
            self.log("STOP command sent.")

    def move_home_safe(self):
        if not self._require_elevator():
            return
        try:
            focus_lower_limit = self._get_focus_lower_limit()
        except ValueError as exc:
            messagebox.showwarning("Focus", str(exc))
            return
        distance_mm = self.safe_home_distance_mm_var.get()
        speed = self.MEDIUM_ELEVATOR_SPEED
        if distance_mm <= 0:
            messagebox.showwarning("Focus", "Safe distance must be > 0 mm.")
            return
        if not self._prepare_new_motion():
            return

        def worker():
            try:
                with self.busy_lock:
                    self.log(f"Moving up by {distance_mm:.3f} mm at speed {speed}.")
                    ok = self._run_focus_move_distance(
                        "up",
                        distance_mm,
                        speed,
                        lower_limit=focus_lower_limit,
                        allow_noop_at_limit=True,
                        stop_event=self.move_stop_event,
                    )
                    if self.move_stop_event.is_set():
                        self.log("Safe-home move cancelled.")
                    else:
                        self.log("Reached safe height." if ok else "Failed to reach safe height.")
            finally:
                self._clear_move_thread_if_current()

        self.move_thread = threading.Thread(target=worker, daemon=True)
        self.move_thread.start()

    # ───────────────────────── Objective ─────────────────────────
    def _read_current_objective(self) -> Optional[int]:
        if not self.nosepiece or not self.nosepiece.is_connected:
            return None
        lens = self.nosepiece.read_position()
        if lens is None:
            # Objective status is asynchronous, so the poller may already have
            # consumed the latest hardware frame. In that case use only the
            # controller's last confirmed lens; never assume the 10x reference.
            lens = self.nosepiece.current_lens
        if lens not in self.OBJECTIVE_FOCUS_OFFSETS_MM:
            return None
        return lens

    def _wait_for_current_objective(self, timeout_s: float,
                                    cancel_event: Optional[threading.Event] = None) -> Optional[int]:
        deadline = time.monotonic() + max(timeout_s, 0.0)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return None
            lens = self._read_current_objective()
            if lens is not None:
                return lens
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                return None
            time.sleep(min(self.OBJECTIVE_READ_RETRY_INTERVAL_S, remaining_s))

    def read_objective_once(self, wait_timeout_s: float = 0.0):
        if not self._require_nosepiece():
            return

        def worker():
            with self.objective_transaction_lock:
                pos = self._wait_for_current_objective(wait_timeout_s)
            if pos is None:
                if wait_timeout_s > 0:
                    self.log(f"Objective read timed out after {wait_timeout_s:.1f} s.")
                else:
                    self.log("Objective read returned no response.")
                return
            label = NosepieceController.MAG_LABELS.get(pos, str(pos))
            self._set_current_lens_var(pos)
            self.log(f"Current objective: {pos} ({label})")

        threading.Thread(target=worker, daemon=True).start()

    def initialize_default_objective(self):
        """At startup, lift 5 mm, select 10x, then lower 5 mm.

        Both focus moves are closed-loop (measured against the elevator's own
        position query, same as the fine-focus and objective-change moves) -
        an earlier open-loop, timing-only version assumed a jog speed
        calibration that turned out to be wrong and drove the stage far past
        5 mm before it could be corrected.
        """
        if not (self.elevator and self.elevator.is_connected):
            self.log("Startup 10x initialization skipped: focus controller is not connected.")
            return
        if not (self.nosepiece and self.nosepiece.is_connected):
            self.log("Startup 10x initialization skipped: objective controller is not connected.")
            return

        with self._startup_objective_lock:
            if self._startup_objective_active:
                self.log("Startup 10x initialization is already running; duplicate request ignored.")
                return
            self._startup_objective_active = True

        if not self._prepare_new_motion():
            with self._startup_objective_lock:
                self._startup_objective_active = False
            return
        stop_event = self.move_stop_event
        target_lens = self.DEFAULT_OBJECTIVE_LENS
        move_distance_mm = self.STARTUP_OBJECTIVE_MOVE_MM
        timeout_s = self.objective_change_timeout_var.get()
        self._set_current_lens_var(None)
        self._show_objective_initialization_popup()

        def worker():
            try:
                with self.busy_lock, self.objective_transaction_lock:
                    lifted = False
                    objective_established = False
                    try:
                        self._set_objective_initialization_status(
                            f"Lifting focus by {move_distance_mm:.3f} mm..."
                        )
                        self.log(f"Startup: lifting focus {move_distance_mm:.3f} mm.")
                        lifted = self._run_focus_move_distance(
                            "up",
                            move_distance_mm,
                            self.OBJECTIVE_ELEVATOR_SPEED,
                            stop_event=stop_event,
                        )
                        if stop_event.is_set():
                            self.log("Startup 10x initialization cancelled during lift.")
                            return
                        if not lifted:
                            self.log("Startup 10x initialization could not complete the 5 mm lift.")
                            return

                        self._set_objective_initialization_status("Selecting the 10x objective...")
                        self.log("Startup: selecting objective 2 (10x).")
                        sent = self.nosepiece.send_lens_command(target_lens)
                        if sent:
                            confirmed = self.nosepiece.wait_for_lens(
                                target_lens,
                                timeout_s=timeout_s,
                                cancel_event=stop_event,
                            )
                            if stop_event.is_set():
                                self.log("Startup 10x initialization cancelled during objective movement.")
                                return
                            if confirmed:
                                self.log("Startup objective confirmed at 10x.")
                            else:
                                # The startup command establishes the software default
                                # even when this controller emits no status frame.
                                self.nosepiece.current_lens = target_lens
                                self.log("No objective status received; assuming commanded 10x position.")
                            self._set_current_lens_var(target_lens)
                            objective_established = True
                        else:
                            self.log("Startup 10x command failed; returning focus to its starting height.")
                    except Exception as exc:
                        self.log(f"Startup 10x initialization error: {exc}")
                    finally:
                        if lifted:
                            if stop_event.is_set():
                                self.log(
                                    "Startup return-down skipped because Stop/cancellation was requested."
                                )
                            else:
                                self._set_objective_initialization_status(
                                    f"Returning focus by {move_distance_mm:.3f} mm..."
                                )
                                self.log(f"Startup: lowering focus {move_distance_mm:.3f} mm.")
                                returned = self._run_focus_move_distance(
                                    "down",
                                    move_distance_mm,
                                    self.OBJECTIVE_ELEVATOR_SPEED,
                                    stop_event=stop_event,
                                )
                                if returned:
                                    if objective_established:
                                        self.log("Startup 10x initialization complete; focus returned 5 mm.")
                                    else:
                                        self.log(
                                            "Startup focus returned 5 mm; 10x objective was not established."
                                        )
                                else:
                                    self.log(
                                        "Startup return-down did not complete; use manual focus controls carefully."
                                    )
            finally:
                with self._startup_objective_lock:
                    self._startup_objective_active = False
                self._close_objective_initialization_popup()
                self._clear_move_thread_if_current()

        try:
            self.move_thread = threading.Thread(target=worker, daemon=True)
            self.move_thread.start()
        except Exception:
            with self._startup_objective_lock:
                self._startup_objective_active = False
            self._close_objective_initialization_popup()
            raise

    def _perform_objective_change(self, target_lens: int, base_lift_distance_mm: float,
                                  timeout_s: float, focus_lower_limit: Optional[float]) -> bool:
        with self.busy_lock, self.objective_transaction_lock:
            speed = self.OBJECTIVE_ELEVATOR_SPEED

            if base_lift_distance_mm <= 0:
                self.log("Lift distance must be > 0 mm.")
                return False
            if target_lens not in NosepieceController.LENS_COMMANDS:
                self.log(f"Unknown objective position: {target_lens}.")
                return False

            current_lens = self._read_current_objective()
            if current_lens is None:
                self.log("Current objective is unknown; objective change cancelled before focus movement.")
                return False

            current_label = NosepieceController.MAG_LABELS[current_lens]
            target_label = NosepieceController.MAG_LABELS[target_lens]
            self._set_current_lens_var(current_lens)

            if current_lens == target_lens:
                self.log(f"Objective is already at {target_lens} ({target_label}); no movement needed.")
                return True

            try:
                up_distance_mm = self._objective_move_distance_mm(base_lift_distance_mm, current_lens)
                down_distance_mm = self._objective_move_distance_mm(base_lift_distance_mm, target_lens)
            except ValueError as exc:
                self.log(str(exc))
                return False

            focus_position = self.elevator.get_current_position()
            self._set_focus_position_var(focus_position)
            if focus_position is None:
                self.log("Focus position is unavailable; objective change cancelled before movement.")
                return False
            clearance_focus_position = focus_position - up_distance_mm
            if clearance_focus_position < ElevatorController.MIN_POSITION_MM:
                self.log(
                    f"Cannot lift {up_distance_mm:.3f} mm from focus position "
                    f"{focus_position:.3f} mm without crossing the top limit."
                )
                return False
            final_focus_position = focus_position - up_distance_mm + down_distance_mm
            if focus_lower_limit is not None and final_focus_position > focus_lower_limit:
                self.log(
                    f"Compensated focus position {final_focus_position:.3f} mm would cross "
                    f"the lowest working limit ({focus_lower_limit:.3f} mm); "
                    "objective change cancelled before movement."
                )
                return False

            current_offset_mm = self.OBJECTIVE_FOCUS_OFFSETS_MM[current_lens]
            self.log(
                f"Changing objective {current_lens} ({current_label}) to "
                f"{target_lens} ({target_label}); lifting {up_distance_mm:.3f} mm "
                f"(10x base {base_lift_distance_mm:.3f} mm, "
                f"offset {current_offset_mm:+.3f} mm)."
            )
            lifted = self.elevator.move_to_position(
                clearance_focus_position,
                speed=speed,
                current_pos=focus_position,
                cancel_event=self.move_stop_event,
            )
            self._set_focus_position_var(self.elevator.current_position)
            if self.move_stop_event.is_set():
                self.log("Objective change cancelled during lift.")
                return False
            if not lifted:
                detail = self.elevator.last_position_error or "unknown elevator error"
                self.log(f"Failed to lift to clearance height: {detail}.")
                return False

            self._set_current_lens_var(None)
            if not self.nosepiece.send_lens_command(target_lens):
                self.log(
                    "Objective command was not confirmed as sent; lens state is uncertain "
                    "and focus remains lifted."
                )
                return False

            changed = self.nosepiece.wait_for_lens(
                target_lens,
                timeout_s=timeout_s,
                cancel_event=self.move_stop_event,
            )
            if self.move_stop_event.is_set():
                self.log("Objective change cancelled while waiting for confirmation; focus remains lifted.")
                return False
            if not changed:
                self.log("Objective change not confirmed before timeout; focus remains at clearance height.")
                return False

            self._set_current_lens_var(target_lens)
            self.log(f"Objective confirmed at {target_lens} ({target_label}).")

            target_offset_mm = self.OBJECTIVE_FOCUS_OFFSETS_MM[target_lens]
            self.log(
                f"Returning to absolute focus position {final_focus_position:.3f} mm "
                f"for {target_label} "
                f"(10x base {base_lift_distance_mm:.3f} mm, "
                f"offset {target_offset_mm:+.3f} mm)."
            )
            returned = self.elevator.move_to_position(
                final_focus_position,
                speed=speed,
                cancel_event=self.move_stop_event,
            )
            self._set_focus_position_var(self.elevator.current_position)
            if self.move_stop_event.is_set():
                self.log("Return-down cancelled after objective change.")
                return False
            if returned:
                self.log(
                    f"Objective change to {target_label} complete at "
                    f"{self.elevator.current_position:.3f} mm."
                )
                return True

            detail = self.elevator.last_position_error or "unknown elevator error"
            self.log(f"Failed to reach the final focus position after objective change: {detail}.")
            return False

    def change_objective(self, target_lens: int):
        if not self._require_elevator() or not self._require_nosepiece():
            return
        try:
            focus_lower_limit = self._get_focus_lower_limit()
        except ValueError as exc:
            messagebox.showwarning("Focus", str(exc))
            return
        if focus_lower_limit is None:
            messagebox.showwarning(
                "Focus",
                "Set a lowest focus limit first (jog down to a safe height above the "
                "sample, then click 'Here' or 'Set' next to Low) before changing "
                "objectives. The return-down move after a lens change has no other "
                "floor and will drive straight into the sample without this limit.",
            )
            return
        lift_distance_mm = self.objective_lift_distance_mm_var.get()
        timeout_s = self.objective_change_timeout_var.get()
        if not self._prepare_new_motion():
            return

        def worker():
            try:
                self._perform_objective_change(
                    target_lens,
                    lift_distance_mm,
                    timeout_s,
                    focus_lower_limit,
                )
            finally:
                self._clear_move_thread_if_current()

        self.move_thread = threading.Thread(target=worker, daemon=True)
        self.move_thread.start()

    def start_objective_polling(self):
        def poller():
            while not self.poll_stop_event.is_set():
                try:
                    if self.objective_transaction_lock.acquire(blocking=False):
                        try:
                            if self.nosepiece and self.nosepiece.is_connected:
                                lens = self.nosepiece.read_position()
                                if lens is not None:
                                    self._set_current_lens_var(lens)
                        finally:
                            self.objective_transaction_lock.release()
                except Exception:
                    pass
                time.sleep(self.OBJECTIVE_POLL_INTERVAL_S)

        threading.Thread(target=poller, daemon=True).start()

    def on_close(self):
        try:
            self._closing = True
            self._destroy_objective_initialization_popup()
            self._cancel_normal_jog_position_read()
            for aid in list(self._ui_after_ids.values()):
                try:
                    self.after_cancel(aid)
                except Exception:
                    pass
            self._ui_after_ids.clear()
            self.poll_stop_event.set()
            self.stop_motion(log_action=False)
            self.disconnect_all()
        except Exception:
            pass
