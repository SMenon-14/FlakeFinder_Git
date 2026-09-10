"""AxisCameraMixin: ToupCam live preview, SDK control, color presets."""
import os
import time
import json
import ctypes
from ctypes import wintypes
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

import cv2
import numpy as np
import pyautogui
from PIL import Image, ImageTk

from src.microscope.onway_camera_sdk import (
    _load_toupcam_sdk_module,
    SDK_PREVIEW_INDEX, SDK_REALTIME_MODE, SDK_USE_MAX_SPEED,
    SDK_FRAME_WAIT_TIMEOUT_S, SDK_FRAME_POLL_DELAY_S,
    SDK_DEFAULT_WB_TEMP, SDK_DEFAULT_WB_TINT, SDK_DEFAULT_HUE,
    SDK_DEFAULT_SATURATION, SDK_DEFAULT_BRIGHTNESS, SDK_DEFAULT_CONTRAST,
    SDK_DEFAULT_GAMMA, SDK_DEFAULT_EXPOSURE_US,
    SDK_HUE_MIN, SDK_HUE_MAX, SDK_SATURATION_MIN, SDK_SATURATION_MAX,
    SDK_BRIGHTNESS_MIN, SDK_BRIGHTNESS_MAX, SDK_CONTRAST_MIN, SDK_CONTRAST_MAX,
    SDK_GAMMA_MIN, SDK_GAMMA_MAX, SDK_EXPOSURE_MIN_US, SDK_EXPOSURE_MAX_US,
    CAMERA_PREVIEW_REFRESH_MS, CAMERA_PREVIEW_MAX_DIM,
    CAMERA_COLOR_PRESETS_PATH, CAMERA_COLOR_PRESET_KEYS, DEFAULT_CAMERA_COLOR_PRESETS,
)


class AxisCameraMixin:
    @staticmethod
    def _format_exposure_us(exposure_us: float) -> str:
        return f"{max(0.0, float(exposure_us)) / 1000.0:.2f} ms"

    def _format_camera_control_value(self, key: str, value: float) -> str:
        if key == "exposure_us":
            return self._format_exposure_us(value)
        return str(int(round(float(value))))

    def _set_camera_control_value(self, key: str, value: float):
        self._camera_control_vars[key].set(value)
        self._camera_control_value_vars[key].set(
            self._format_camera_control_value(key, value)
        )

    def _apply_camera_exposure_range(self, minimum_us: int, maximum_us: int):
        minimum_us = max(1, int(minimum_us))
        maximum_us = max(minimum_us, int(maximum_us))
        self._camera_exposure_range_us = (minimum_us, maximum_us)

        slider = self._camera_slider_widgets.get("exposure_us")
        if slider is not None:
            slider.configure(from_=minimum_us, to=40000)

        current = float(self._camera_control_vars["exposure_us"].get())
        clamped = min(max(current, minimum_us), maximum_us)
        if clamped != current:
            self._set_camera_control_value("exposure_us", clamped)

    def _sync_camera_exposure_from_sdk(self, cam=None) -> int | None:
        """Read the camera's actual exposure time and move the slider to match."""
        if cam is None:
            with self._sdk_lock:
                cam = self._sdk_camera
        if cam is None:
            return None
        try:
            exposure_us = int(cam.get_RealExpoTime())
        except Exception:
            try:
                exposure_us = int(cam.get_ExpoTime())
            except Exception:
                return None
        minimum_us, maximum_us = self._camera_exposure_range_us
        clamped = min(max(exposure_us, minimum_us), maximum_us)
        self._set_camera_control_value("exposure_us", clamped)
        return clamped

    def _update_camera_exposure_controls(self):
        slider = self._camera_slider_widgets.get("exposure_us")
        if slider is None:
            return
        with self._sdk_lock:
            camera_open = self._sdk_camera is not None and not self._sdk_disconnected
        if camera_open and not self.camera_auto_exposure_var.get():
            slider.state(["!disabled"])
        else:
            slider.state(["disabled"])

    def _on_camera_auto_exposure_toggled(self):
        enabled = bool(self.camera_auto_exposure_var.get())
        with self._sdk_lock:
            cam = self._sdk_camera
        if cam is None:
            if not self.start_live_preview(
                show_error=True,
                on_ready=self._on_camera_auto_exposure_toggled,
            ):
                self._update_camera_exposure_controls()
            return
        if cam is None:
            self._update_camera_exposure_controls()
            return

        try:
            self._sync_camera_exposure_from_sdk(cam)

            cam.put_AutoExpoEnable(1 if enabled else 0)
            if not enabled:
                self._apply_camera_slider_value("exposure_us")
            self._refresh_camera_control_values()
            self._set_camera_status(
                "Auto exposure enabled." if enabled else "Auto exposure disabled."
            )
        except Exception as e:
            self.camera_auto_exposure_var.set(not enabled)
            self._update_camera_exposure_controls()
            messagebox.showerror("Auto exposure", str(e))
            return

        self._update_camera_exposure_controls()


    # ── ImageView automation ─────────────────────────────────────────────────

    def _auto_start_camera_preview(self):
        self.start_live_preview(show_error=False)

    def _request_camera_preview_refresh(self):
        if self._camera_preview_after_id is None and self.winfo_exists():
            self._camera_preview_after_id = self.after(0, self._refresh_camera_preview)

    def _set_camera_status(self, text: str):
        self.camera_status_var.set(text)

    def _show_camera_placeholder(self, text: str):
        if not hasattr(self, "camera_preview_label"):
            return
        self._camera_preview_photo = None
        self.camera_preview_label.configure(
            image="", text=text,
            bg="#0D1117", fg="#4A8FCC",
        )

    def _refresh_camera_preview(self):
        self._camera_preview_after_id = None
        if not self.winfo_exists():
            return

        self._sync_camera_ui_from_sdk_state()

        with self._sdk_lock:
            exposure_needs_sync = self._camera_exposure_needs_sync
            self._camera_exposure_needs_sync = False
            frame_count = self._sdk_frame_counter
            capture_sequence = self._camera_capture_sequence
            camera_open = self._sdk_camera is not None and not self._sdk_disconnected
        if exposure_needs_sync:
            self._sync_camera_exposure_from_sdk()

        processed = self._camera_preview_worker.latest_result_after(
            self._camera_preview_last_result_count
        )
        if processed is not None:
            processed_count, prepared_image, processing_error = processed
            self._camera_preview_last_result_count = processed_count
            if processing_error is not None:
                self.tlog(f"[SDK] Preview processing error: {processing_error}")
            elif prepared_image is not None:
                self._display_prepared_camera_frame(prepared_image)
                self._camera_preview_last_frame_count = processed_count

        if frame_count <= 0:
            self._camera_preview_last_frame_count = 0
            if camera_open:
                self._show_camera_placeholder("Waiting for microscope frames...")
            else:
                self._show_camera_placeholder(
                    "Microscope preview idle.\nClick Start Live to open the camera."
                )
        elif capture_sequence > self._camera_preview_last_submitted_count:
            # Submit only the newest captured frame. LatestItemWorker overwrites
            # a pending older request when processing cannot keep up.
            try:
                frame = self._sdk_latest_frame_bgr()
            except Exception:
                frame = None

            if frame is not None and hasattr(self, "camera_preview_label"):
                target_w = max(320, int(self.camera_preview_label.winfo_width() or 0))
                target_h = max(240, int(self.camera_preview_label.winfo_height() or 0))
                with self._sdk_lock:
                    width = self._sdk_width
                    height = self._sdk_height
                    device_name = self._sdk_device_name
                overlay = f"{device_name or 'Microscope'}  {width}x{height}"
                if self._camera_preview_worker.submit(
                    capture_sequence, (frame, target_w, target_h, overlay)
                ):
                    self._camera_preview_last_submitted_count = capture_sequence

        if self.winfo_exists():
            self._camera_preview_after_id = self.after(
                CAMERA_PREVIEW_REFRESH_MS,
                self._refresh_camera_preview,
            )

    @staticmethod
    def _prepare_camera_preview(payload):
        """Resize, overlay, letterbox, and convert a frame away from Tk."""
        frame_bgr, target_w, target_h, overlay_text = payload
        frame_h, frame_w = frame_bgr.shape[:2]
        scale = min(
            target_w / float(frame_w),
            target_h / float(frame_h),
            CAMERA_PREVIEW_MAX_DIM / float(max(frame_w, frame_h)),
        )
        scale = max(scale, 1e-6)
        new_w = max(1, int(frame_w * scale))
        new_h = max(1, int(frame_h * scale))

        # Resize on the smaller BGR array first (cv2 is much faster than PIL's
        # LANCZOS here), then convert + overlay only the resized frame.
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        resized_bgr = cv2.resize(frame_bgr, (new_w, new_h), interpolation=interp)

        if overlay_text:
            cv2.putText(
                resized_bgr,
                overlay_text,
                (10, 16),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.4,
                (0, 255, 0),
                1,
                cv2.LINE_AA,
            )

        rgb = cv2.cvtColor(resized_bgr, cv2.COLOR_BGR2RGB)

        if new_w == target_w and new_h == target_h:
            canvas_arr = rgb
        else:
            x0 = (target_w - new_w) // 2
            y0 = (target_h - new_h) // 2
            canvas_arr = np.empty((target_h, target_w, 3), dtype=np.uint8)
            canvas_arr[:, :] = (0x0D, 0x11, 0x17)
            canvas_arr[y0:y0 + new_h, x0:x0 + new_w] = rgb

        return Image.fromarray(canvas_arr)

    def _display_prepared_camera_frame(self, prepared_image: Image.Image):
        # ImageTk must remain on Tk, but all expensive pixel work is complete.
        if not hasattr(self, "camera_preview_label"):
            return
        if not self.camera_preview_label.winfo_exists():
            return
        photo = ImageTk.PhotoImage(prepared_image)

        if self.camera_preview_label.winfo_exists():
            self._camera_preview_photo = photo
            self.camera_preview_label.configure(image=photo, text="")


    def _sync_camera_ui_from_sdk_state(self, force: bool = False):
        with self._sdk_lock:
            if not force and not self._camera_ui_needs_sync:
                return
            disconnected = self._sdk_disconnected
            camera_open = self._sdk_camera is not None and not disconnected
            device_name = self._sdk_device_name
            preview_index = self._sdk_preview_index
            width = self._sdk_width
            height = self._sdk_height
            choices = list(self._sdk_resolution_choices)
            self._camera_ui_needs_sync = False

        labels = [f"{idx}: {w} x {h}" for idx, w, h in choices]
        self._camera_resolution_index_by_label = {
            label: idx
            for label, (idx, _w, _h) in zip(labels, choices)
        }
        if hasattr(self, "camera_resolution_combo"):
            self.camera_resolution_combo.configure(values=labels)
        if labels:
            selected_label = next(
                (label for label, idx in self._camera_resolution_index_by_label.items()
                 if idx == preview_index),
                labels[0],
            )
            self.camera_resolution_var.set(selected_label)
        else:
            self.camera_resolution_var.set("")

        if disconnected:
            self._set_camera_status("Microscope camera disconnected.")
        elif camera_open:
            self._set_camera_status(
                f"Live microscope ready: {device_name} at {width}x{height}."
            )
            self._refresh_camera_control_values()
        else:
            self._set_camera_status(
                "Microscope preview idle. Click Start Live to open the camera."
            )
        self._update_camera_exposure_controls()

    def _on_camera_resolution_selected(self, _event=None):
        label = self.camera_resolution_var.get().strip()
        if not label:
            return
        new_index = self._camera_resolution_index_by_label.get(label)
        if new_index is None:
            return
        if self.scan_running or self._scan_start_pending:
            self._set_camera_status(
                "Stop the scan before changing microscope resolution."
            )
            self._sync_camera_ui_from_sdk_state(force=True)
            return
        if new_index == self._sdk_requested_resolution_index:
            return

        self._sdk_requested_resolution_index = new_index
        self._camera_preview_requested = True
        self._set_camera_status("Restarting microscope camera...")
        try:
            future = self._request_sdk_camera_restart()
        except Exception as exc:
            messagebox.showerror("Microscope Preview", str(exc))
            return
        self._watch_camera_open(future, show_error=True)

    def _refresh_camera_control_values(self):
        with self._sdk_lock:
            cam = self._sdk_camera
        if cam is None:
            return

        getter_map = {
            "brightness": cam.get_Brightness,
            "contrast": cam.get_Contrast,
            "saturation": cam.get_Saturation,
            "gamma": cam.get_Gamma,
            "hue": cam.get_Hue,
        }
        for key, getter in getter_map.items():
            try:
                value = int(getter())
            except Exception:
                continue
            self._camera_control_vars[key].set(value)
            self._camera_control_value_vars[key].set(str(value))

        try:
            min_exp, max_exp, _default_exp = cam.get_ExpTimeRange()
            self._apply_camera_exposure_range(min_exp, max_exp)
        except Exception:
            pass

        try:
            auto_exposure = bool(cam.get_AutoExpoEnable())
            self.camera_auto_exposure_var.set(auto_exposure)
        except Exception:
            pass

        self._sync_camera_exposure_from_sdk(cam)

        self._update_camera_exposure_controls()

    def _on_camera_slider_changed(self, key: str):
        value = self._camera_control_vars[key].get()
        self._camera_control_value_vars[key].set(
            self._format_camera_control_value(key, value)
        )
        aid = self._camera_slider_after_ids.pop(key, None)
        if aid:
            try:
                self.after_cancel(aid)
            except Exception:
                pass
        if self.winfo_exists():
            self._camera_slider_after_ids[key] = self.after(
                80,
                lambda k=key: self._apply_camera_slider_value(k),
            )

    def _apply_camera_slider_value(self, key: str):
        self._camera_slider_after_ids.pop(key, None)
        with self._sdk_lock:
            cam = self._sdk_camera
        if cam is None:
            return

        value = int(round(self._camera_control_vars[key].get()))
        if key == "exposure_us":
            minimum_us, maximum_us = self._camera_exposure_range_us
            value = min(max(value, minimum_us), maximum_us)
            self._set_camera_control_value(key, value)
            if self.camera_auto_exposure_var.get():
                return
        setter_map = {
            "brightness": cam.put_Brightness,
            "contrast": cam.put_Contrast,
            "saturation": cam.put_Saturation,
            "gamma": cam.put_Gamma,
            "hue": cam.put_Hue,
            "exposure_us": cam.put_ExpoTime,
        }
        try:
            setter_map[key](value)
        except Exception as e:
            self.tlog(f"[SDK] {key} set failed: {e}")

    def _load_camera_color_presets(self) -> dict[str, dict[str, float]]:
        try:
            with open(CAMERA_COLOR_PRESETS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            self._save_camera_color_presets_to_disk(DEFAULT_CAMERA_COLOR_PRESETS)
            return {name: dict(values) for name, values in DEFAULT_CAMERA_COLOR_PRESETS.items()}
        except Exception as e:
            self.tlog(f"[Presets] Failed to load camera color presets: {e}")
            return {}

        presets: dict[str, dict[str, float]] = {}
        if isinstance(data, dict):
            for name, values in data.items():
                if isinstance(values, dict):
                    presets[name] = {
                        key: values[key] for key in CAMERA_COLOR_PRESET_KEYS if key in values
                    }
        return presets

    def _save_camera_color_presets_to_disk(self, presets: dict[str, dict[str, float]] | None = None):
        if presets is None:
            presets = self._camera_color_presets
        try:
            with open(CAMERA_COLOR_PRESETS_PATH, "w", encoding="utf-8") as f:
                json.dump(presets, f, indent=2)
        except Exception as e:
            self.tlog(f"[Presets] Failed to save camera color presets: {e}")

    def _refresh_camera_preset_choices(self):
        if self.camera_preset_combo is None:
            return
        names = sorted(self._camera_color_presets.keys())
        self.camera_preset_combo["values"] = names
        if self.camera_preset_var.get() not in names:
            self.camera_preset_var.set(names[0] if names else "")

    def _save_camera_color_preset(self):
        name = simpledialog.askstring(
            "Save Color Preset",
            "Preset name:",
            initialvalue=self.camera_preset_var.get(),
            parent=self,
        )
        if name is None:
            return
        name = name.strip()
        if not name:
            return
        if name in self._camera_color_presets and not messagebox.askyesno(
            "Overwrite Preset",
            f"Preset '{name}' already exists. Overwrite it?",
            parent=self,
        ):
            return

        self._camera_color_presets[name] = {
            key: int(round(self._camera_control_vars[key].get()))
            for key in CAMERA_COLOR_PRESET_KEYS
        }
        self._save_camera_color_presets_to_disk()
        self._refresh_camera_preset_choices()
        self.camera_preset_var.set(name)
        self.tlog(f"[Presets] Saved camera color preset '{name}'.")

    def _apply_selected_camera_preset(self):
        name = self.camera_preset_var.get()
        preset = self._camera_color_presets.get(name)
        if not preset:
            return
        for key, value in preset.items():
            if key not in self._camera_control_vars:
                continue
            self._camera_control_vars[key].set(value)
            self._camera_control_value_vars[key].set(str(int(round(value))))
            aid = self._camera_slider_after_ids.pop(key, None)
            if aid:
                try:
                    self.after_cancel(aid)
                except Exception:
                    pass
            self._apply_camera_slider_value(key)
        self.tlog(f"[Presets] Applied camera color preset '{name}'.")

    def _delete_selected_camera_preset(self):
        name = self.camera_preset_var.get()
        if not name or name not in self._camera_color_presets:
            return
        if not messagebox.askyesno(
            "Delete Preset", f"Delete camera color preset '{name}'?", parent=self
        ):
            return
        del self._camera_color_presets[name]
        self._save_camera_color_presets_to_disk()
        self._refresh_camera_preset_choices()
        self.tlog(f"[Presets] Deleted camera color preset '{name}'.")

    def start_live_preview(
        self, show_error: bool = True, on_ready=None, on_error=None
    ) -> bool:
        """Request camera startup and return immediately to the Tk event loop."""
        self._camera_preview_requested = True
        with self._camera_open_lock:
            close_pending = (
                self._camera_close_future is not None
                and not self._camera_close_future.done()
            )
        with self._sdk_lock:
            already_open = (
                self._sdk_camera is not None
                and not self._sdk_disconnected
                and not close_pending
            )
        if already_open:
            self._sync_camera_ui_from_sdk_state(force=True)
            self._request_camera_preview_refresh()
            if on_ready is not None:
                self.after(0, on_ready)
            return True

        try:
            future = self._request_sdk_camera_open()
        except Exception as e:
            self._set_camera_status(str(e))
            if show_error:
                messagebox.showerror("Microscope Preview", str(e))
            if on_error is not None:
                on_error(e)
            return False
        self._set_camera_status("Opening microscope camera...")
        self._watch_camera_open(
            future,
            show_error=show_error,
            on_ready=on_ready,
            on_error=on_error,
        )
        self._request_camera_preview_refresh()
        return True

    def _watch_camera_open(
        self, future, *, show_error: bool, on_ready=None, on_error=None
    ):
        def _done(completed):
            def _finish():
                try:
                    completed.result()
                except Exception as exc:
                    message = str(exc)
                    self._set_camera_status(message)
                    if show_error and self.winfo_exists():
                        messagebox.showerror("Microscope Preview", message)
                    if on_error is not None:
                        on_error(exc)
                    return
                if not self._camera_preview_requested:
                    try:
                        self._camera_lifecycle_executor.submit(self._close_sdk_camera)
                    except Exception:
                        pass
                    return
                self._sync_camera_ui_from_sdk_state(force=True)
                self._request_camera_preview_refresh()
                if on_ready is not None:
                    on_ready()

            self._post_ui_task(_finish)

        future.add_done_callback(_done)

    def stop_live_preview(self):
        if self.scan_running or self._scan_start_pending:
            self._set_camera_status("Stop the scan before closing the microscope preview.")
            return
        self._camera_preview_requested = False
        self._set_camera_status("Closing microscope camera...")
        try:
            with self._camera_open_lock:
                future = self._camera_lifecycle_executor.submit(self._close_sdk_camera)
                self._camera_close_future = future
        except Exception as exc:
            self._set_camera_status(str(exc))
            return

        def _closed(_completed):
            def _finish():
                if self._camera_preview_requested:
                    return
                self._sync_camera_ui_from_sdk_state(force=True)
                self._show_camera_placeholder(
                    "Microscope preview stopped.\nClick Start Live to reopen the camera."
                )

            self._post_ui_task(_finish)

        future.add_done_callback(_closed)

    def _current_snapshot_default_name(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if self.controller.connected:
            try:
                a_val, b_val = self._cached_ab()
                return f"microscope_{stamp}_A_{a_val:.3f}_B_{b_val:.3f}.png"
            except Exception:
                pass
        return f"microscope_{stamp}.png"

    def _current_live_frame_for_ui(self, require_open: bool = False) -> np.ndarray:
        if require_open:
            with self._sdk_lock:
                camera_open = (
                    self._sdk_camera is not None and not self._sdk_disconnected
                )
            if not camera_open:
                raise RuntimeError("Microscope camera is still opening.")
        return self._sdk_latest_frame_bgr()

    @staticmethod
    def _write_image_file(path: str, frame_bgr: np.ndarray) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        if not cv2.imwrite(path, frame_bgr):
            raise RuntimeError(f"Failed to save {path}")
        return path

    def _submit_image_write(self, path: str, frame_bgr: np.ndarray):
        """Queue encoded image output on the serialized camera writer."""
        return self._camera_writer_executor.submit(
            self._write_image_file, path, frame_bgr
        )

    def snap_and_save_as(self):
        with self._sdk_lock:
            camera_open = self._sdk_camera is not None and not self._sdk_disconnected
        if not camera_open:
            self.start_live_preview(show_error=True, on_ready=self.snap_and_save_as)
            return
        try:
            frame_bgr = self._sdk_latest_frame_bgr()
            initial_dir = (self.scan_folder_var.get() or "").strip().strip('\"')
            if not initial_dir or "://" in initial_dir:
                initial_dir = os.path.expanduser("~")
            path = filedialog.asksaveasfilename(
                parent=self.winfo_toplevel(),
                title="Save microscope snapshot",
                initialdir=initial_dir,
                initialfile=self._current_snapshot_default_name(),
                defaultextension=".png",
                filetypes=[
                    ("PNG image", "*.png"),
                    ("JPEG image", "*.jpg"),
                    ("TIFF image", "*.tif"),
                    ("Bitmap image", "*.bmp"),
                    ("All files", "*.*"),
                ],
            )
            if not path:
                return
        except Exception as e:
            messagebox.showerror("Save microscope snapshot", str(e))
            return

        self._set_camera_status(f"Saving microscope snapshot to {path}...")
        future = self._submit_image_write(path, frame_bgr)

        def _written(completed):
            def _finish():
                try:
                    completed.result()
                except Exception as exc:
                    messagebox.showerror("Save microscope snapshot", str(exc))
                    self._set_camera_status(f"Snapshot save failed: {exc}")
                    return
                self.log(f"[CAMERA] Saved snapshot to {path}")
                self._set_camera_status(f"Saved microscope snapshot to {path}")

            self._post_ui_task(_finish)

        future.add_done_callback(_written)

    def _auto_white_balance(self):
        with self._sdk_lock:
            cam = self._sdk_camera
            is_mono = self._sdk_is_mono
        if cam is None:
            self.start_live_preview(show_error=True, on_ready=self._auto_white_balance)
            return
        if cam is None:
            return
        if is_mono:
            self._set_camera_status("Auto white balance is not available on a mono camera.")
            return
        try:
            cam.AwbOnce()
            self._set_camera_status("Auto white balance requested.")
        except Exception as e:
            messagebox.showerror("Auto white balance", str(e))

    def _reset_camera_look(self):
        with self._sdk_lock:
            camera_open = self._sdk_camera is not None and not self._sdk_disconnected
        if not camera_open:
            self.start_live_preview(show_error=True, on_ready=self._reset_camera_look)
            return
        try:
            self._apply_sdk_default_look()
            self._refresh_camera_control_values()
            self._set_camera_status("Microscope color settings reset.")
        except Exception as e:
            messagebox.showerror("Reset microscope look", str(e))

    def _on_snap_hotkey_changed(self, *_):
        return

    def _configure_global_snap_hotkey(self):
        return

    def _on_global_snap_hotkey(self):
        return

    def _auto_save_after_manual_snap(self):
        return

    @staticmethod
    def _sdk_camera_callback(n_event, ctx):
        ctx._handle_sdk_camera_event(n_event)

    @staticmethod
    def _sdk_buffer_to_bgr(live_buf, width: int, height: int, row_pitch: int) -> np.ndarray:
        raw = bytes(live_buf.raw[:row_pitch * height])
        arr = np.frombuffer(raw, dtype=np.uint8).reshape((height, row_pitch))
        rgb = arr[:, : width * 3].reshape((height, width, 3))
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

    def _handle_sdk_camera_event(self, n_event):
        with self._sdk_lock:
            sdk = self._sdk_toupcam
            cam = self._sdk_camera
            live_buf = self._sdk_live_buf
            width = self._sdk_width
            height = self._sdk_height
            row_pitch = self._sdk_row_pitch
        if sdk is None or cam is None or live_buf is None:
            return

        try:
            if n_event == sdk.TOUPCAM_EVENT_IMAGE:
                cam.PullImageV4(live_buf, 0, 24, 0, None)
                now = time.monotonic()
                with self._sdk_lock:
                    if (
                        now - self._camera_last_publish_monotonic
                        < self._camera_publish_interval_s
                    ):
                        return
                    self._camera_last_publish_monotonic = now
                frame_bgr = self._sdk_buffer_to_bgr(live_buf, width, height, row_pitch)
                with self._sdk_lock:
                    self._sdk_latest_frame_bgr_cache = frame_bgr
                    self._sdk_frame_counter += 1
                    self._camera_capture_sequence += 1
                    self._sdk_frame_event.set()
            elif n_event == sdk.TOUPCAM_EVENT_EXPOSURE:
                with self._sdk_lock:
                    self._camera_exposure_needs_sync = True
            elif n_event == sdk.TOUPCAM_EVENT_DISCONNECTED:
                with self._sdk_lock:
                    self._sdk_disconnected = True
                    self._camera_ui_needs_sync = True
                    self._sdk_frame_event.set()
                self.tlog("[SDK] Camera disconnected.")
            elif n_event == getattr(sdk, "TOUPCAM_EVENT_NOFRAMETIMEOUT", -1):
                self.tlog("[SDK] No frame timeout.")
        except Exception as e:
            self.tlog(f"[SDK] callback error: {e}")

    def _apply_sdk_default_look(self):
        with self._sdk_lock:
            cam = self._sdk_camera
            is_mono = self._sdk_is_mono
        if cam is None:
            return

        ops = [
            ("Hue", lambda: cam.put_Hue(SDK_DEFAULT_HUE)),
            ("Saturation", lambda: cam.put_Saturation(SDK_DEFAULT_SATURATION)),
            ("Brightness", lambda: cam.put_Brightness(SDK_DEFAULT_BRIGHTNESS)),
            ("Contrast", lambda: cam.put_Contrast(SDK_DEFAULT_CONTRAST)),
            ("Gamma", lambda: cam.put_Gamma(SDK_DEFAULT_GAMMA)),
        ]
        if not is_mono:
            ops.append(
                ("TempTint", lambda: cam.put_TempTint(SDK_DEFAULT_WB_TEMP, SDK_DEFAULT_WB_TINT))
            )
        for label, fn in ops:
            try:
                fn()
            except Exception as e:
                self.tlog(f"[SDK] {label} default warning: {e}")

    def _sdk_open_error_message(self, exc: Exception) -> str:
        msg = str(exc).strip() or exc.__class__.__name__
        lower_msg = msg.lower()
        if "resource is in use" in lower_msg or "busy" in lower_msg:
            return (
                "Direct ToupCam capture could not start because the camera is already in use. "
                "Close ImageView or any other camera software, then try again."
            )
        if "catastrophic failure" in lower_msg:
            return (
                "Direct ToupCam capture failed during camera startup. "
                "This camera rejects some SDK startup options on this machine. "
                "The unsupported color-control startup calls have been disabled; if this persists, "
                "close ImageView and retry."
            )
        return f"Failed to start direct ToupCam capture: {msg}"

    def _request_sdk_camera_open(self):
        """Return the shared background-open future, creating it if needed."""
        with self._camera_open_lock:
            if self._camera_pipeline_shutdown:
                raise RuntimeError("Camera pipeline is shut down.")
            future = self._camera_open_future
            if future is None or future.done():
                future = self._camera_lifecycle_executor.submit(
                    self._open_sdk_camera_impl
                )
                self._camera_open_future = future
            return future

    def _request_sdk_camera_restart(self):
        with self._camera_open_lock:
            if self._camera_pipeline_shutdown:
                raise RuntimeError("Camera pipeline is shut down.")

            def _restart():
                self._close_sdk_camera()
                return self._open_sdk_camera_impl()

            future = self._camera_lifecycle_executor.submit(_restart)
            self._camera_open_future = future
            return future

    def _ensure_sdk_camera_open(self):
        """Blocking helper for scan/background workers that require a camera."""
        with self._sdk_lock:
            if self._sdk_camera is not None and not self._sdk_disconnected:
                return
        self._request_sdk_camera_open().result()

    def _open_sdk_camera_impl(self):
        with self._sdk_lock:
            if self._sdk_camera is not None and not self._sdk_disconnected:
                return

        sdk = _load_toupcam_sdk_module()
        devices = sdk.Toupcam.EnumV2()
        if not devices:
            raise RuntimeError("No ToupCam SDK cameras found.")

        dev = devices[0]
        cam = sdk.Toupcam.Open(dev.id)
        if cam is None:
            raise RuntimeError(f"Failed to open SDK camera: {dev.displayname}")

        try:
            cam.put_Option(sdk.TOUPCAM_OPTION_BYTEORDER, 0)
            cam.put_HFlip(True)
            realtime_mode = 0
            try:
                cam.put_RealTime(SDK_REALTIME_MODE)
                realtime_mode = SDK_REALTIME_MODE
            except Exception as exc:
                self.tlog(f"[SDK] Could not enable real-time camera mode: {exc}")

            speed_level = None
            max_speed_level = int(dev.model.maxspeed)
            if SDK_USE_MAX_SPEED:
                try:
                    cam.put_Speed(max_speed_level)
                    speed_level = int(cam.get_Speed())
                except Exception as exc:
                    self.tlog(f"[SDK] Could not select maximum camera speed: {exc}")
            try:
                cam.put_AutoExpoEnable(0)
            except Exception:
                pass
            preview_index = min(
                max(0, int(self._sdk_requested_resolution_index)),
                max(0, dev.model.preview - 1),
            )
            if dev.model.preview > 0:
                cam.put_eSize(preview_index)
            width, height = cam.get_Size()
            row_pitch = sdk.TDIBWIDTHBYTES(width * 24)
            live_buf = ctypes.create_string_buffer(row_pitch * height)
            resolution_choices = [
                (idx, int(res.width), int(res.height))
                for idx, res in enumerate(dev.model.res[:dev.model.preview])
            ]

            with self._sdk_lock:
                self._sdk_toupcam = sdk
                self._sdk_camera = cam
                self._sdk_live_buf = live_buf
                self._sdk_width = width
                self._sdk_height = height
                self._sdk_row_pitch = row_pitch
                self._sdk_frame_counter = 0
                self._camera_last_publish_monotonic = 0.0
                self._sdk_disconnected = False
                self._sdk_latest_frame_bgr_cache = None
                self._sdk_device_name = dev.displayname
                self._sdk_preview_index = preview_index
                self._sdk_resolution_choices = resolution_choices
                self._sdk_is_mono = bool(dev.model.flag & sdk.TOUPCAM_FLAG_MONO)
                self._camera_ui_needs_sync = True
                self._sdk_frame_event.clear()
            cam.StartPullModeWithCallback(self._sdk_camera_callback, self)
            self._apply_sdk_default_look()
            self.tlog(
                f"[SDK] Opened {dev.displayname} "
                f"preview[{preview_index}]={width}x{height}, "
                f"real-time={realtime_mode}, "
                f"speed={speed_level if speed_level is not None else 'default'}"
                f"/{max_speed_level}"
            )
            self._wait_for_sdk_frame(-1, SDK_FRAME_WAIT_TIMEOUT_S)
        except Exception as e:
            try:
                cam.Close()
            except Exception:
                pass
            with self._sdk_lock:
                self._sdk_toupcam = None
                self._sdk_camera = None
                self._sdk_live_buf = None
                self._sdk_width = 0
                self._sdk_height = 0
                self._sdk_row_pitch = 0
                self._sdk_frame_counter = 0
                self._camera_last_publish_monotonic = 0.0
                self._sdk_disconnected = False
                self._sdk_latest_frame_bgr_cache = None
                self._sdk_device_name = ""
                self._sdk_preview_index = -1
                self._sdk_resolution_choices = []
                self._sdk_is_mono = False
                self._camera_ui_needs_sync = True
                self._sdk_frame_event.clear()
            raise RuntimeError(self._sdk_open_error_message(e)) from e

    def _close_sdk_camera(self):
        with self._sdk_lock:
            cam = self._sdk_camera
            self._sdk_toupcam = None
            self._sdk_camera = None
            self._sdk_live_buf = None
            self._sdk_width = 0
            self._sdk_height = 0
            self._sdk_row_pitch = 0
            self._sdk_frame_counter = 0
            self._camera_last_publish_monotonic = 0.0
            self._sdk_disconnected = False
            self._sdk_latest_frame_bgr_cache = None
            self._sdk_device_name = ""
            self._sdk_preview_index = -1
            self._sdk_resolution_choices = []
            self._sdk_is_mono = False
            self._camera_ui_needs_sync = True
            self._sdk_frame_event.clear()
        if cam is not None:
            try:
                cam.Close()
            except Exception:
                pass

    def _shutdown_camera_pipeline(self):
        """Stop camera background work during application shutdown."""
        self._camera_preview_requested = False
        with self._camera_open_lock:
            if self._camera_pipeline_shutdown:
                return
            self._camera_pipeline_shutdown = True

        try:
            close_future = self._camera_lifecycle_executor.submit(
                self._close_sdk_camera
            )
            close_future.result(timeout=3.0)
        except Exception as exc:
            self.tlog(f"[SDK] Camera close warning: {exc}")

        self._camera_preview_worker.shutdown(timeout=1.0)
        self._camera_lifecycle_executor.shutdown(wait=False, cancel_futures=True)
        self._camera_writer_executor.shutdown(wait=False, cancel_futures=True)

    def _wait_for_sdk_frame(self, baseline_count: int, timeout_s: float) -> int:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._sdk_lock:
                current = self._sdk_frame_counter
                disconnected = self._sdk_disconnected
            if disconnected:
                raise RuntimeError("SDK camera disconnected.")
            if current > baseline_count:
                return current

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._sdk_frame_event.wait(min(SDK_FRAME_POLL_DELAY_S, remaining))
            self._sdk_frame_event.clear()

        raise TimeoutError(f"No fresh SDK frame received after {timeout_s:.1f}s.")

    def _sdk_latest_frame_bgr(self):
        # The callback thread only ever *replaces* this reference (never
        # mutates it in place), and every caller here is read-only, so it's
        # safe to hand back the cached array directly without copying it.
        with self._sdk_lock:
            frame = self._sdk_latest_frame_bgr_cache
            frame_count = self._sdk_frame_counter

        if frame is None or frame_count <= 0:
            raise RuntimeError("No SDK frame available.")
        return frame
