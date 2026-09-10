"""AxisScanMixin: snap/save automation, edge detection, corner-guided wafer scan."""
import os
import sys
import time
import math
import re
import threading
import ctypes
from ctypes import wintypes
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import cv2
import numpy as np
import pyautogui
import pyperclip
from src.microscope.autofocus import FocusCalculator
from src.autoscan.autoscan_popup import ScannerUI

from src.microscope.onway_motion_hardware import (
    AXIS_INDEX, SCAN_CORNER_ORDER,
    DEFAULT_JOG_ACCEL, MAX_EDGE_PROBE_STEPS, MIN_HITS_PER_ROW,
    DEFAULT_PINK_FRACTION_THRESHOLD, DEFAULT_PINK_CONNECTED_THRESHOLD,
    SAVE_WAIT_TIMEOUT_S, SAVE_WAIT_POLL_DELAY_S, SAVE_WAIT_STABLE_POLLS,
    IMAGE_READ_RETRIES, IMAGE_READ_RETRY_DELAY_S, SAVE_RETRY_ATTEMPTS,
    SAVE_DIALOG_OPEN_DELAY_S, SAVE_PATH_PASTE_DELAY_S, SAVE_SUBMIT_DELAY_S,
    SAVE_OVERWRITE_CONFIRM_DELAY_S, SAVE_RETRY_BACKOFF_S, SAVE_CLOSE_WINDOW_DELAY_S,
    SCAN_MOVE_TIMEOUT_S, SCAN_MOVE_POLL_DELAY_S, SCAN_MOVE_STABLE_POLLS,
)
from src.microscope.onway_camera_sdk import SDK_FRAME_WAIT_TIMEOUT_S
from src.microscope.onway_microscope_panel import MicroscopeControlPanel


class AxisScanMixin:
    SCAN_LAPLACIAN_STEP_MM = 0.005
    SCAN_LAPLACIAN_SETTLE_S = 0.1

    def _snap(self) -> bool:
        hk = self.snap_hotkey_var.get().strip().lower()
        if not hk:
            return False
        try:
            parts = [p.strip() for p in hk.split("+") if p.strip()]
            if len(parts) > 1:
                pyautogui.hotkey(*parts)
            else:
                pyautogui.press(hk)
            return True
        except Exception:
            return False

    def _save_as(self, full_path: str, overwrite: bool = True):
        """Ctrl+S → paste path → Enter.  ImageView must be in the foreground."""
        full_path = os.path.abspath(full_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        preexisting = os.path.exists(full_path)
        pyautogui.hotkey("ctrl", "s")
        time.sleep(SAVE_DIALOG_OPEN_DELAY_S)
        pyperclip.copy(full_path)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.hotkey("ctrl", "v")
        time.sleep(SAVE_PATH_PASTE_DELAY_S)
        pyautogui.press("enter")
        time.sleep(SAVE_SUBMIT_DELAY_S)
        if overwrite and preexisting:
            pyautogui.press("enter")
            time.sleep(SAVE_OVERWRITE_CONFIRM_DELAY_S)

    def _save_as_and_wait(self, full_path: str, overwrite: bool = True) -> str:
        """
        Run the ImageView Save As automation and wait for the file to appear.
        Retries the same Ctrl+S flow a few times because the dialog timing is fragile.
        """
        last_error = None
        for attempt in range(1, SAVE_RETRY_ATTEMPTS + 1):
            save_started_wall = time.time()
            self.tlog(
                f"[SAVE] attempt {attempt}/{SAVE_RETRY_ATTEMPTS} "
                f"{os.path.basename(full_path)}"
            )
            try:
                self._save_as(full_path, overwrite=overwrite)
                saved = self._wait_for_saved_image(full_path, save_started_wall)
                pyautogui.hotkey("ctrl", "w")
                time.sleep(SAVE_CLOSE_WINDOW_DELAY_S)
                return saved
            except Exception as e:
                last_error = e
                self.tlog(f"[SAVE] attempt {attempt} failed: {e}")
                time.sleep(SAVE_RETRY_BACKOFF_S)
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"Could not save image: {full_path}")

    def _capture_active_window_image(self, full_path: str) -> bool:
        """
        Save the currently focused window to `full_path`.
        This avoids relying on ImageView's Save As dialog during scanning.
        """
        try:
            full_path = os.path.abspath(full_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)

            window = None
            try:
                window = pyautogui.getActiveWindow()
            except Exception:
                window = None

            title = ""
            if window is not None:
                try:
                    title = (window.title or "").strip()
                except Exception:
                    title = ""

            # If the control app still has focus, don't save a screenshot of it.
            if title and ("ONWAY" in title or "Axis Control" in title):
                self.tlog(f"[CAPTURE] Active window is not ImageView: {title}")
                return False

            region = None
            if window is not None:
                try:
                    hwnd = getattr(window, "_hWnd", None)
                    if hwnd:
                        client_rect = wintypes.RECT()
                        if ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(client_rect)):
                            origin = wintypes.POINT(0, 0)
                            if ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(origin)):
                                width = int(client_rect.right - client_rect.left)
                                height = int(client_rect.bottom - client_rect.top)
                                if width > 50 and height > 50:
                                    region = (int(origin.x), int(origin.y), width, height)
                    if region is None:
                        left = int(window.left)
                        top = int(window.top)
                        width = int(window.width)
                        height = int(window.height)
                        if width > 50 and height > 50:
                            region = (left, top, width, height)
                except Exception:
                    region = None

            shot = pyautogui.screenshot(region=region)
            shot.save(full_path)
            self.tlog(
                f"[CAPTURE] Saved "
                f"{'active window client area' if region else 'screen'} image to "
                f"{os.path.basename(full_path)}"
            )
            return True
        except Exception as e:
            self.tlog(f"[CAPTURE] fallback screenshot failed: {e}")
            return False

    def _wait_for_saved_image(self, expected_path: str,
                              save_started_wall: float) -> str:
        """
        Wait until the expected image exists and its size stops changing.
        If ImageView saves under a different name, fall back to the newest
        image in the target folder created after this save started.
        """
        expected_path = os.path.abspath(expected_path)
        folder = os.path.dirname(expected_path)
        deadline = time.monotonic() + SAVE_WAIT_TIMEOUT_S
        last_size = -1
        stable_polls = 0

        while time.monotonic() < deadline:
            if os.path.isfile(expected_path):
                try:
                    st = os.stat(expected_path)
                    size = st.st_size
                except OSError:
                    size = -1
                    st = None
                if size > 0 and st is not None and st.st_mtime + 0.25 >= save_started_wall:
                    stable_polls = stable_polls + 1 if size == last_size else 0
                    last_size = size
                    if stable_polls >= SAVE_WAIT_STABLE_POLLS:
                        return expected_path

            fallback = None
            newest_mtime = float("-inf")
            try:
                for name in os.listdir(folder):
                    cand = os.path.join(folder, name)
                    if not os.path.isfile(cand):
                        continue
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}:
                        continue
                    try:
                        st = os.stat(cand)
                    except OSError:
                        continue
                    if st.st_mtime + 0.25 < save_started_wall or st.st_size <= 0:
                        continue
                    if st.st_mtime > newest_mtime:
                        newest_mtime = st.st_mtime
                        fallback = cand
            except OSError:
                pass

            if fallback is not None:
                return fallback

            time.sleep(SAVE_WAIT_POLL_DELAY_S)

        raise FileNotFoundError(
            f"Saved image not found after {SAVE_WAIT_TIMEOUT_S:.1f}s: {expected_path}"
        )

    # ── Edge detection ───────────────────────────────────────────────────────

    def _has_wafer_content(
        self,
        image_path: str,
        pink_fraction_threshold: float = DEFAULT_PINK_FRACTION_THRESHOLD,
        pink_connected_threshold: float = DEFAULT_PINK_CONNECTED_THRESHOLD,
    ) -> bool:
        """True when a meaningful amount of the pink wafer region is present."""
        try:
            img_bgr = None
            for _ in range(IMAGE_READ_RETRIES):
                img_bgr = cv2.imread(image_path, cv2.IMREAD_COLOR)
                if img_bgr is not None:
                    break
                time.sleep(IMAGE_READ_RETRY_DELAY_S)
            if img_bgr is None:
                self.tlog(f"[EDGE] Cannot read: {image_path}")
                raise FileNotFoundError(image_path)

            hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
            h, s, v = cv2.split(hsv)
            b, g, r = cv2.split(img_bgr)

            r16 = r.astype(np.int16)
            g16 = g.astype(np.int16)
            b16 = b.astype(np.int16)

            # Pink wafer region: red and blue both elevated, with green lower.
            pink_rgb = (
                (r16 > g16 + 12) &
                (b16 > g16 + 8) &
                (np.abs(r16 - b16) < 90) &
                (r16 > 70) &
                (b16 > 60)
            )
            pink_hsv = (
                (h >= 120) &
                (h <= 179) &
                (s >= 10) &
                (v >= 40)
            )
            pink_mask = np.where(pink_rgb | pink_hsv, 255, 0).astype(np.uint8)
            kernel = np.ones((5, 5), np.uint8)
            pink_mask = cv2.morphologyEx(pink_mask, cv2.MORPH_OPEN, kernel)
            pink_mask = cv2.morphologyEx(pink_mask, cv2.MORPH_CLOSE, kernel)

            pink_fraction = float(np.count_nonzero(pink_mask)) / float(pink_mask.size)

            n_labels, _, stats, _ = cv2.connectedComponentsWithStats(pink_mask, 8)
            largest_component = 0
            if n_labels > 1:
                largest_component = int(stats[1:, cv2.CC_STAT_AREA].max())
            largest_fraction = float(largest_component) / float(pink_mask.size)

            self.tlog(
                f"[EDGE] {os.path.basename(image_path)} "
                f"pink_frac={pink_fraction:.5f} "
                f"largest_frac={largest_fraction:.5f}"
            )
            return (
                pink_fraction >= pink_fraction_threshold or
                largest_fraction >= pink_connected_threshold
            )
        except Exception as e:
            self.tlog(f"[EDGE] error: {e}")
            raise

    def _save_current_image(self, folder: str, count_ref: list[int],
                            overwrite: bool,
                            capture_delay_s: float | None = None) -> tuple[str, str]:
        """Save the current SDK camera image into `folder` using the generated name."""
        self._ensure_sdk_camera_open()
        with self._sdk_lock:
            baseline_count = self._sdk_frame_counter

        capture_delay = (
            float(self.scan_capture_delay_s_var.get())
            if capture_delay_s is None
            else float(capture_delay_s)
        )
        if capture_delay > 0:
            time.sleep(capture_delay)

        try:
            self._wait_for_sdk_frame(baseline_count, SDK_FRAME_WAIT_TIMEOUT_S)
        except TimeoutError:
            self.tlog("[SDK] No fresh frame after capture delay; using latest available frame.")

        frame_bgr = self._sdk_latest_frame_bgr()
        _, pa = self.controller.get_pos(AXIS_INDEX["A"])
        _, pb = self.controller.get_pos(AXIS_INDEX["B"])
        try:
            # mmddyy_sample_num_(xcoord, ycoord)_zoom.png
            fname = f"{self.exfoliated_date}_{self.scan_sample_num}_({pa:.3f}, {pb:.3f})_{self.zoom_lvl}.png"
        except AttributeError:
            # S_samplenum_A_xcoord_B_ycoord
            fname = f"S_{count_ref[0]:04d}_A_{pa:.3f}_B_{pb:.3f}.png"
        full = os.path.abspath(os.path.join(folder, fname))

        if os.path.exists(full) and not overwrite:
            raise FileExistsError(f"File already exists: {full}")
        self._submit_image_write(full, frame_bgr).result()

        count_ref[0] += 1
        return full, fname

    def _snap_and_save(self, folder: str, count_ref: list[int],
                       overwrite: bool, trigger_snap: bool = True,
                       capture_delay_s: float | None = None) -> tuple[str, str]:
        """Snap, save, return (full_path, fname).  count_ref[0] is incremented."""
        print("snapped")
        return self._save_current_image(folder=folder,
                                        count_ref=count_ref,
                                        overwrite=overwrite,
                                        capture_delay_s=capture_delay_s)

    def _next_scan_image_index(self, folder: str) -> int:
        max_idx = 0
        try:
            for name in os.listdir(folder):
                m = re.match(r"^S_(\d+)_", name)
                if m:
                    max_idx = max(max_idx, int(m.group(1)))
        except Exception:
            pass
        return max_idx + 1

    def snap_and_save_once(self):
        with self._sdk_lock:
            camera_open = self._sdk_camera is not None and not self._sdk_disconnected
        if not camera_open:
            self.start_live_preview(show_error=True, on_ready=self.snap_and_save_once)
            return
        try:
            if not self.controller.connected:
                raise ValueError("Connect to the motion controller first.")
            folder = self._normalize_folder(self.scan_folder_var.get())
            self.scan_folder_var.set(folder)
            overwrite = bool(self.scan_overwrite_var.get())
            capture_delay = float(self.scan_capture_delay_s_var.get())
        except Exception as e:
            messagebox.showerror("Snap + Save", str(e))
            return

        self._set_camera_status("Capturing microscope snapshot...")

        def _worker():
            try:
                count_ref = [self._next_scan_image_index(folder)]
                _full, fname = self._snap_and_save(
                    folder=folder,
                    count_ref=count_ref,
                    overwrite=overwrite,
                    capture_delay_s=capture_delay,
                )
            except Exception as exc:
                self._post_ui_task(
                    lambda error=exc: messagebox.showerror("Snap + Save", str(error))
                )
                return

            def _saved():
                self.log(f"[SAVE] Saved {fname} to {folder}")
                self._set_camera_status(f"Saved microscope snapshot: {fname}")

            self._post_ui_task(_saved)

        threading.Thread(
            target=_worker,
            name="camera-manual-snapshot",
            daemon=True,
        ).start()

    def _current_ab(self) -> tuple[float, float]:
        _, a = self.controller.get_pos(AXIS_INDEX["A"])
        _, b = self.controller.get_pos(AXIS_INDEX["B"])
        return float(a), float(b)

    def _cached_ab(self) -> tuple[float, float]:
        """Read the UI polling snapshot without making a Tk-thread DLL call."""
        snapshot = self.controller.position_snapshot()
        samples = [snapshot.get(AXIS_INDEX[key]) for key in ("A", "B")]
        if any(sample is None or sample.error is not None for sample in samples):
            raise RuntimeError(
                "No valid A/B position has been measured yet. Wait for the "
                "stage readout to update, then try again."
            )
        return float(samples[0].value), float(samples[1].value)

    def _scan_position_tolerance(self, tile: float) -> float:
        return max(0.005, min(0.05, abs(tile) * 0.05))

    def _wait_for_ab_target(
        self,
        target_a: float,
        target_b: float,
        tolerance: float,
        label: str,
    ) -> tuple[float, float]:
        deadline = time.monotonic() + SCAN_MOVE_TIMEOUT_S
        stable_polls = 0
        last_a = float("nan")
        last_b = float("nan")

        while time.monotonic() < deadline:
            if not self.scan_running:
                raise RuntimeError(f"Scan stopped while waiting for {label}.")

            current_a, current_b = self._current_ab()
            last_a, last_b = current_a, current_b

            if (
                abs(current_a - target_a) <= tolerance and
                abs(current_b - target_b) <= tolerance
            ):
                stable_polls += 1
                if stable_polls >= SCAN_MOVE_STABLE_POLLS:
                    return current_a, current_b
            else:
                stable_polls = 0

            time.sleep(SCAN_MOVE_POLL_DELAY_S)

        raise TimeoutError(
            f"{label} not reached within {SCAN_MOVE_TIMEOUT_S:.1f}s: "
            f"target A={target_a:.4f} B={target_b:.4f}, "
            f"actual A={last_a:.4f} B={last_b:.4f}"
        )

    def _move_ab_and_confirm(
        self,
        target_a: float,
        target_b: float,
        settle: float,
        tolerance: float,
        label: str,
    ) -> tuple[float, float]:
        self.controller.move_abs(AXIS_INDEX["B"], target_b)
        self.controller.move_abs(AXIS_INDEX["A"], target_a)
        current_a, current_b = self._wait_for_ab_target(
            target_a=target_a,
            target_b=target_b,
            tolerance=tolerance,
            label=label,
        )
        if settle > 0:
            time.sleep(settle)
        self.tlog(
            f"[SCAN] {label} confirmed @ "
            f"A={current_a:.4f} B={current_b:.4f}"
        )
        return current_a, current_b

    def _move_focus_to(self, target_z: float, label: str = "") -> None:
        elevator = self.micro_panel.elevator
        if not (elevator and elevator.is_connected):
            return
        ok = elevator.move_to_position(target_z, speed=MicroscopeControlPanel.MEDIUM_ELEVATOR_SPEED)
        self.micro_panel._set_focus_position_var(elevator.current_position)
        if not ok:
            suffix = f" ({label})" if label else ""
            self.tlog(f"[SCAN] Focus move to Z={target_z:.3f} failed{suffix}.")

    def _scan_laplacian_score(self, af: FocusCalculator) -> float:
        """Settle, then snap+score. The settle here is in addition to the camera's own
        fresh-frame wait inside `_autofocus_snap`."""
        time.sleep(self.SCAN_LAPLACIAN_SETTLE_S)
        frame = self._autofocus_snap()
        return af.get_absolute_focus_score_laplacian(frame)

    def _adjust_focus_laplacian(self, label: str = "") -> None:
        """Fine-tune focus immediately before a scan snapshot: sample two steps above
        and two steps below the current position (plus the current position itself),
        then move to whichever of those five locations scored best."""
        af = getattr(self, "_scan_focus_calculator", None)
        if af is None:
            return
        elevator = self.micro_panel.elevator
        if not (elevator and elevator.is_connected):
            return

        current_pos = elevator.current_position
        if current_pos is None:
            current_pos = elevator.get_current_position()
        if current_pos is None:
            self.tlog(f"[SCAN] Laplacian focus skipped ({label}): position unavailable.")
            return

        step = self.SCAN_LAPLACIAN_STEP_MM
        sweep_positions = [current_pos + offset * step for offset in (-1, 0, 1)]

        best_pos = current_pos
        best_score = None
        for pos in sweep_positions:
            if not self.scan_running:
                break
            ok = elevator.move_to_position(pos, speed=MicroscopeControlPanel.FINE_ELEVATOR_SPEED)
            self.micro_panel._set_focus_position_var(elevator.current_position)
            if not ok:
                continue
            score = self._scan_laplacian_score(af)
            self.tlog(f"[SCAN] Laplacian focus {label}: pos={pos:.3f} score={score:.1f}")
            if best_score is None or score > best_score:
                best_score = score
                best_pos = pos

        elevator.move_to_position(best_pos, speed=MicroscopeControlPanel.FINE_ELEVATOR_SPEED)
        self.micro_panel._set_focus_position_var(elevator.current_position)
        if best_score is not None:
            self.tlog(f"[SCAN] Laplacian focus {label}: best pos={best_pos:.3f} score={best_score:.1f}")

    def _record_scan_corner(self, key: str):
        try:
            if not self.controller.connected:
                raise ValueError("Connect to the motion controller first.")
            elevator = self.micro_panel.elevator
            if not (elevator and elevator.is_connected):
                raise ValueError("Connect the focus elevator first.")
            a, b = self._cached_ab()
            z = elevator.current_position
            if z is None:
                z = elevator.get_current_position()
            if z is None:
                raise ValueError(
                    "Could not read focus position from the elevator. "
                    "Click 'Read' on the Focus / Objective panel, or jog focus "
                    "up/down once, then try recording the corner again."
                )
            self.scan_corner_vars[key]["A"].set(f"{a:.6f}")
            self.scan_corner_vars[key]["B"].set(f"{b:.6f}")
            self.scan_corner_vars[key]["Z"].set(f"{z:.3f}")
            self.micro_panel._set_focus_position_var(z)
            self.log(f"[SCAN] Recorded {key}: A={a:.6f} B={b:.6f} Z={z:.3f}")
        except Exception as e:
            messagebox.showerror("Record Scan Corner", str(e))

    def _read_scan_corners(self) -> dict[str, tuple[float, float, float]]:
        corners: dict[str, tuple[float, float, float]] = {}
        for label, key in SCAN_CORNER_ORDER:
            try:
                a = float(self.scan_corner_vars[key]["A"].get().strip())
                b = float(self.scan_corner_vars[key]["B"].get().strip())
                z = float(self.scan_corner_vars[key]["Z"].get().strip())
            except Exception as e:
                raise ValueError(f"Record or enter valid A/B/Z values for {label}.") from e
            corners[key] = (a, b, z)
        return corners

    def _row_fraction_values(self, corners: dict[str, tuple[float, float, float]],
                             tile: float, row_limit: int) -> list[float]:
        top_left = corners["TL"]
        top_right = corners["TR"]
        bottom_left = corners["BL"]
        bottom_right = corners["BR"]

        vertical_span = max(
            abs(bottom_left[1] - top_left[1]),
            abs(bottom_right[1] - top_right[1]),
            abs(((bottom_left[1] + bottom_right[1]) * 0.5) -
                ((top_left[1] + top_right[1]) * 0.5)),
        )
        if vertical_span <= 0:
            raise ValueError("Top and bottom corners must have different B values.")

        row_count = int(math.ceil(vertical_span / tile)) + 1
        row_count = max(1, row_count)
        if row_limit > 0:
            row_count = min(row_count, row_limit)

        if row_count == 1:
            return [0.0]
        return [i / float(row_count - 1) for i in range(row_count)]

    def _interpolate_scan_row(self, corners: dict[str, tuple[float, float, float]],
                              t: float) -> tuple[float, float, float, float, float]:
        tl_a, tl_b, tl_z = corners["TL"]
        tr_a, tr_b, tr_z = corners["TR"]
        bl_a, bl_b, bl_z = corners["BL"]
        br_a, br_b, br_z = corners["BR"]

        left_a = tl_a + (bl_a - tl_a) * t
        left_b = tl_b + (bl_b - tl_b) * t
        right_a = tr_a + (br_a - tr_a) * t
        right_b = tr_b + (br_b - tr_b) * t
        row_b = 0.5 * (left_b + right_b)

        left_z = tl_z + (bl_z - tl_z) * t
        right_z = tr_z + (br_z - tr_z) * t
        return left_a, right_a, row_b, left_z, right_z

    def _interpolated_focus_z(self, left_a: float, right_a: float,
                              left_z: float, right_z: float,
                              current_a: float) -> float:
        """Bilinear focus height for a position along an interpolated scan row."""
        span = right_a - left_a
        if abs(span) < 1e-9:
            return left_z
        s = (current_a - left_a) / span
        return left_z + (right_z - left_z) * s

    def _axis_positions_between(self, start: float, end: float,
                                step: float) -> list[float]:
        if step <= 0:
            raise ValueError("Tile step must be > 0")
        if abs(end - start) < 1e-9:
            return [float(start)]

        positions = [float(start)]
        direction = 1.0 if end > start else -1.0
        current = float(start)

        while abs(end - current) > step:
            current += direction * step
            positions.append(current)

        if abs(positions[-1] - end) > 1e-6:
            positions.append(float(end))
        return positions

    def _planned_corner_scan_positions(
        self,
        corners: dict[str, tuple[float, float, float]],
        tile: float,
        row_limit: int,
    ) -> tuple[list[float], int]:
        row_fractions = self._row_fraction_values(corners, tile, row_limit)
        total_positions = 0
        for t in row_fractions:
            left_a, right_a, _row_b, _left_z, _right_z = self._interpolate_scan_row(corners, t)
            total_positions += len(self._axis_positions_between(left_a, right_a, tile))
        return row_fractions, total_positions

    def _probe_for_edge(
        self,
        axis_key: str,
        start_pos: float,
        direction: int,          # +1 or -1
        step: float,
        max_steps: int,
        settle: float,
        folder: str,
        count_ref: list[int],
        overwrite: bool,
    ) -> float | None:
        """
        Step along axis_key in `direction`, snapping at each step.
        Return the first position where the image has wafer content, else None.
        """
        current = start_pos
        for _ in range(max_steps):
            if not self.scan_running:
                return None

            full, fname = self._snap_and_save(folder, count_ref, overwrite)
            self.tlog(f"[PROBE] saved {fname}")

            if self._has_wafer_content(full):
                self.tlog(f"[PROBE] Edge found at {axis_key}={current:.4f}")
                return current

            current += direction * step
            self.controller.move_abs(AXIS_INDEX[axis_key], current)
            time.sleep(settle)

        self.tlog(f"[PROBE] No edge found after {max_steps} steps.")
        return None

    # ── Wafer scan public API ─────────────────────────────────────────────────

    def _begin_pending_camera_scan(self, start_message: str):
        if not self._scan_start_pending:
            return
        self._scan_start_pending = False
        self.scan_running = True
        self.scan_thread = threading.Thread(
            target=self._scan_worker_corner,
            name="corner-guided-scan",
            daemon=True,
        )
        self.scan_thread.start()
        self.log(start_message)

    def _camera_scan_open_failed(self, exc: Exception):
        self._scan_start_pending = False
        self.log(f"[SCAN] Camera startup failed; scan was not started: {exc}")

    def start_corner_guided_scan(self):
        if self.scan_running or self._scan_start_pending:
            self.log("[SCAN] Already running.")
            return
        if not self.controller.connected:
            messagebox.showwarning("Not connected",
                                   "Connect to the motion controller first.")
            return
        if not (self.micro_panel.elevator and self.micro_panel.elevator.is_connected):
            messagebox.showwarning("Not connected",
                                   "Connect the focus elevator first.")
            return
        try:
            folder = self._normalize_folder(self.scan_folder_var.get())
            self.launch_and_wait_for_scanner(folder)
            self.scan_folder_var.set(folder)
            self._read_scan_corners()
            start_delay = float(self.scan_start_delay_s_var.get())
            capture_delay = float(self.scan_capture_delay_s_var.get())
            self._scan_focus_calculator = FocusCalculator(step_size=self.SCAN_LAPLACIAN_STEP_MM)
            if start_delay < 0:
                raise ValueError("Start delay must be >= 0")
            if capture_delay < 0:
                raise ValueError("Capture delay must be >= 0")
        except Exception as e:
            messagebox.showwarning("Scan setup", str(e))
            return
        self._scan_start_pending = True
        started = self.start_live_preview(
            show_error=True,
            on_ready=lambda: self._begin_pending_camera_scan(
                f"[SCAN] Started. SDK camera capture will begin after {start_delay:.1f}s."
            ),
            on_error=self._camera_scan_open_failed,
        )
        if not started:
            self._scan_start_pending = False
            return
        self.log("[SCAN] Waiting for microscope camera startup...")

    def launch_and_wait_for_scanner(self, folder):
        print("Opening configuration popup...")
        parent_window = self._root() 
        
        self.popup = ScannerUI(parent=parent_window, default_folder=folder)
        self.popup.transient(parent_window)
        self.popup.grab_set()  
        
        # 1. Create a native Tkinter event gate variable
        popup_wait_gate = tk.BooleanVar(value=False)
        self.popup._wait_gate = popup_wait_gate
        self.popup.submitted = False
        
        print("Code execution paused safely. Waiting for user submission...")
        # 2. FIX: Wait for the variable to flip instead of waiting for window destruction
        parent_window.wait_variable(popup_wait_gate)
            
        print("Popup wait gate released! Resuming execution...")
        
        # 3. Read variables safely from your application memory state
        if hasattr(self, "popup") and getattr(self.popup, "submitted", False):
            self.exfoliated_date = self.popup.get_exfoliated_date()
            self.zoom_lvl = self.popup.get_zoom_level()
            self.scan_sample_num = self.popup.get_sample_number()
            print(f"Parameters loaded successfully: Date={self.exfoliated_date}, Zoom={self.zoom_lvl}")
        else:
            print("Popup window was closed manually without submitting parameters.")

        # 4. Reinstate your microscope camera preview loop safely
        if hasattr(self, "camera_preview_label") and self.camera_preview_label.winfo_exists():
            print("Reactivating camera loop...")
            parent_window.update_idletasks() 
            self._request_camera_preview_refresh()





    def start_wafer_scan(self):
        if self.scan_running or self._scan_start_pending:
            self.log("[SCAN] Already running.")
            return
        if not self.controller.connected:
            messagebox.showwarning("Not connected",
                                   "Connect to the motion controller first.")
            return
        if not (self.micro_panel.elevator and self.micro_panel.elevator.is_connected):
            messagebox.showwarning("Not connected",
                                   "Connect the focus elevator first.")
            return
        try:
            folder = self._normalize_folder(self.scan_folder_var.get())
            self.launch_and_wait_for_scanner(folder)
            self.scan_folder_var.set(folder)
            self._read_scan_corners()
        except Exception as e:
            messagebox.showwarning("Scan setup", str(e))
            return
        self._scan_start_pending = True
        started = self.start_live_preview(
            show_error=True,
            on_ready=lambda: self._begin_pending_camera_scan(
                "[SCAN] Started — direct SDK camera capture enabled."
            ),
            on_error=self._camera_scan_open_failed,
        )
        if not started:
            self._scan_start_pending = False
            return
        self.log("[SCAN] Waiting for microscope camera startup...")

    def stop_wafer_scan(self):
        self._scan_start_pending = False
        self.scan_running = False
        try:
            self.popup.shutdown()
        except Exception:
            pass
        self.log("[SCAN] Stop requested.")

    # ── Wafer scan worker ─────────────────────────────────────────────────────

    def _scan_worker(self):
        """
        Live-edge snake scan algorithm
        ────────────────────────────────
        For each row:
          1. Probe inward (from just outside last-known boundary) to find the
             leading edge of the wafer.
          2. Sweep in `direction` tile by tile, snapping at each position,
             until the trailing edge is detected (blank image).
          3. Step B down by one tile, flip direction → next row.

        Stops when:
          - A row yields < MIN_HITS_PER_ROW on-wafer tiles
            (B has passed the wafer).
          - scan_running is set to False.
          - max_rows is reached.
        """
        try:
            tile   = float(self.scan_size_mm_var.get())
            n_rows = int(self.scan_n_var.get())
            settle = float(self.scan_settle_s_var.get())
            ovr    = bool(self.scan_overwrite_var.get())
            folder = self._normalize_folder(self.scan_folder_var.get())

            if tile <= 0:
                raise ValueError("Tile step must be > 0")

            _, a0 = self.controller.get_pos(AXIS_INDEX["A"])
            _, b0 = self.controller.get_pos(AXIS_INDEX["B"])

            self.tlog(
                f"[SCAN] Start  A={a0:.4f}  B={b0:.4f}  "
                f"tile={tile:.4f}  max_rows={n_rows}"
            )

            count_ref = [self._next_scan_image_index(folder)]  # shared mutable counter
            direction = +1         # +1 = sweep right first
            current_b = b0

            # Track wafer extents from the previous row so probing starts nearby
            prev_left_a  = a0
            prev_right_a = a0

            for row in range(n_rows):
                if not self.scan_running:
                    break

                self.tlog(
                    f"[SCAN] ── Row {row + 1}  "
                    f"B={current_b:.4f}  "
                    f"dir={'→' if direction > 0 else '←'}"
                )

                # ── Move to row B ──────────────────────────────────────────
                self.controller.move_abs(AXIS_INDEX["B"], current_b)
                time.sleep(settle)

                # ── Probe for leading edge ─────────────────────────────────
                # Start one tile outside the previous row's near boundary so
                # we don't miss if the wafer widens slightly.
                if direction > 0:
                    probe_start = prev_left_a - tile
                else:
                    probe_start = prev_right_a + tile

                self.controller.move_abs(AXIS_INDEX["A"], probe_start)
                time.sleep(settle)

                edge_a = self._probe_for_edge(
                    axis_key  = "A",
                    start_pos = probe_start,
                    direction = direction,
                    step      = tile,
                    max_steps = MAX_EDGE_PROBE_STEPS,
                    settle    = settle,
                    folder    = folder,
                    count_ref = count_ref,
                    overwrite = ovr,
                )

                if edge_a is None:
                    self.tlog("[SCAN] Leading edge not found — scan complete.")
                    break

                current_a = edge_a
                row_hits  = 1   # probe already captured one on-wafer image

                # Update near boundary
                if direction > 0:
                    prev_left_a = current_a
                else:
                    prev_right_a = current_a

                # ── Sweep across until trailing edge ───────────────────────
                while self.scan_running:
                    next_a = current_a + direction * tile
                    self.controller.move_abs(AXIS_INDEX["A"], next_a)
                    time.sleep(settle)
                    current_a = next_a

                    full, fname = self._snap_and_save(folder, count_ref, ovr)
                    self.tlog(f"[SCAN] saved {fname}")

                    if not self._has_wafer_content(full):
                        self.tlog(
                            f"[SCAN] Trailing edge at A={current_a:.4f}  "
                            f"hits={row_hits}"
                        )
                        # Update far boundary (last confirmed on-wafer position)
                        if direction > 0:
                            prev_right_a = current_a - tile
                        else:
                            prev_left_a  = current_a + tile
                        break

                    row_hits += 1

                # ── Validate row ───────────────────────────────────────────
                if row_hits < MIN_HITS_PER_ROW:
                    self.tlog(
                        f"[SCAN] Row {row + 1}: only {row_hits} hit(s) — "
                        "past wafer. Stopping."
                    )
                    break

                self.tlog(
                    f"[SCAN] Row {row + 1} complete — "
                    f"{row_hits} tiles captured."
                )

                # ── Advance B + reverse direction ──────────────────────────
                current_b += tile
                direction  *= -1

            self.tlog(f"[SCAN] Finished.  Total images: {count_ref[0] - 1}")

        except Exception as e:
            self.tlog(f"[SCAN] ERROR: {e}")
        finally:
            self.scan_running = False

    # ── Clean shutdown ────────────────────────────────────────────────────────

    def _scan_worker_corner(self):
        """Corner-guided snake scan within the recorded wafer quadrilateral. At each
        shot, focus is fine-tuned with a Laplacian directional search immediately
        before the final snapshot is saved."""
        try:
            tile = float(self.scan_size_mm_var.get())
            n_rows = int(self.scan_n_var.get())
            settle = float(self.scan_settle_s_var.get())
            start_delay = float(self.scan_start_delay_s_var.get())
            ovr = bool(self.scan_overwrite_var.get())
            folder = self._normalize_folder(self.scan_folder_var.get())
            corners = self._read_scan_corners()

            if tile <= 0:
                raise ValueError("Tile step must be > 0")

            position_tolerance = self._scan_position_tolerance(tile)
            row_fractions, planned_positions = self._planned_corner_scan_positions(
                corners, tile, n_rows
            )
            if not row_fractions:
                raise ValueError("No scan rows were generated from the recorded corners.")

            self.tlog(
                f"[SCAN] Corner scan start  tile={tile:.4f}  "
                f"rows={len(row_fractions)}  "
                f"planned_positions={planned_positions}  "
                f"max_rows={'auto' if n_rows <= 0 else n_rows}"
            )

            initial_a, initial_b, initial_z = corners["TL"]
            self.tlog(
                f"[SCAN] Moving to recorded Top Left  "
                f"A={initial_a:.4f} B={initial_b:.4f} Z={initial_z:.3f}"
            )
            self._move_ab_and_confirm(
                target_a=initial_a,
                target_b=initial_b,
                settle=settle,
                tolerance=position_tolerance,
                label="Top Left start position",
            )
            self._move_focus_to(initial_z, label="Top Left start position")

            if start_delay > 0:
                self.tlog(
                    f"[SCAN] Waiting {start_delay:.1f}s before first SDK capture."
                )
                time.sleep(start_delay)
                if not self.scan_running:
                    return

            count_ref = [self._next_scan_image_index(folder)]
            direction = +1

            for row_idx, t in enumerate(row_fractions, start=1):
                if not self.scan_running:
                    break

                left_a, right_a, row_b, left_z, right_z = self._interpolate_scan_row(corners, t)
                row_positions = self._axis_positions_between(left_a, right_a, tile)
                if direction < 0:
                    row_positions = list(reversed(row_positions))

                self.tlog(
                    f"[SCAN] Row {row_idx}/{len(row_fractions)}  "
                    f"B={row_b:.4f}  "
                    f"A={row_positions[0]:.4f}->{row_positions[-1]:.4f}  "
                    f"Z={left_z:.3f}->{right_z:.3f}  "
                    f"shots={len(row_positions)}"
                )

                self._move_ab_and_confirm(
                    target_a=row_positions[0],
                    target_b=row_b,
                    settle=settle,
                    tolerance=position_tolerance,
                    label=f"Row {row_idx} start",
                )
                self._move_focus_to(
                    self._interpolated_focus_z(left_a, right_a, left_z, right_z, row_positions[0]),
                    label=f"Row {row_idx} start",
                )

                for shot_idx, current_a in enumerate(row_positions, start=1):
                    if not self.scan_running:
                        break
                    if shot_idx > 1:
                        self.controller.move_abs(AXIS_INDEX["A"], current_a)
                        target_z = self._interpolated_focus_z(left_a, right_a, left_z, right_z, current_a)
                        self._move_focus_to(target_z, label=f"Row {row_idx} shot {shot_idx}")
                        time.sleep(settle)

                    _, current_b = self._current_ab()
                    self._adjust_focus_laplacian(label=f"Row {row_idx} shot {shot_idx}")
                    current_z = self.micro_panel.elevator.current_position
                    z_text = f"{current_z:.3f}" if current_z is not None else "--"
                    full, fname = self._snap_and_save(folder, count_ref, ovr)
                    self.tlog(
                        f"[SCAN] saved {fname} @ "
                        f"A={current_a:.4f} B={current_b:.4f} Z={z_text} "
                        f"({shot_idx}/{len(row_positions)})"
                    )

                if not self.scan_running:
                    break
                direction *= -1

            self.tlog(f"[SCAN] Finished.  Total saved files: {count_ref[0] - 1}")

        except Exception as e:
            self.tlog(f"[SCAN] ERROR: {e}")
        finally:
            self.scan_running = False
            #shut down server
            self.popup.shutdown()
