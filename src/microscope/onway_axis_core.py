"""AxisControlCore: connection, UI shell, keyboard jog, rotation/AB, readout loop."""
import os
import sys
import time
import math
import threading
import queue
from concurrent.futures import ThreadPoolExecutor
import keyboard
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from src.microscope.onway_app_config import APP_NAVY, APP_SUCCESS, APP_DANGER, APP_HDR_FG
from src.microscope.onway_motion_hardware import (
    MCCController, AXIS_INDEX, DEFAULT_AXIS_SPEED, DEFAULT_PORT,
    DEFAULT_DLL_PATH, SCAN_CORNER_ORDER, DEFAULT_SPEED_PARAM_INDEX, DEFAULT_JOG_ACCEL
)
from src.microscope.onway_motion_service import (
    MotionService, PRIORITY_INTERACTIVE
)
from src.microscope.onway_microscope_panel import MicroscopeControlPanel
from src.microscope.onway_temperature_panel import CompactTemperaturePanel
from src.microscope.onway_camera_sdk import (
    SDK_PREVIEW_INDEX,
    SDK_DEFAULT_HUE, SDK_DEFAULT_SATURATION, SDK_DEFAULT_BRIGHTNESS,
    SDK_DEFAULT_CONTRAST, SDK_DEFAULT_GAMMA, SDK_DEFAULT_EXPOSURE_US,
    SDK_HUE_MIN, SDK_HUE_MAX, SDK_SATURATION_MIN, SDK_SATURATION_MAX,
    SDK_BRIGHTNESS_MIN, SDK_BRIGHTNESS_MAX, SDK_CONTRAST_MIN, SDK_CONTRAST_MAX,
    SDK_GAMMA_MIN, SDK_GAMMA_MAX, SDK_EXPOSURE_MIN_US, SDK_EXPOSURE_MAX_US,
    SDK_FRAME_WAIT_TIMEOUT_S,
)
from src.microscope.onway_camera_pipeline import LatestItemWorker


class AxisControlCore(tk.Frame):
    """
    Complete axis / motion control panel.
    Embed inside any Tk container, then call on_close() when the window closes.
    """

    # ── Construction ─────────────────────────────────────────────────────────

    def __init__(self, master, connections_in_header: bool = False):
        super().__init__(master)
        self.pack(fill="both", expand=True)
        self.connections_in_header = connections_in_header

        self.controller = MotionService(
            MCCController(),
            poll_axes={key: AXIS_INDEX[key] for key in ("A", "B", "C")
                       if key in AXIS_INDEX},
            poll_interval_s=0.5,
        )

        # Connection
        self.var_dll  = tk.StringVar(value=DEFAULT_DLL_PATH)
        self.var_port = tk.StringVar(value=DEFAULT_PORT)

        # Keyboard jog
        self.kb_enabled = tk.BooleanVar(value=False)
        self._pressed: set[str] = set()
        self._jog_after_id: dict[int, bool] = {}
        self._jog_dt     = 0.01   # seconds per timed-jog slice

        # Speed
        self.speed_xy = tk.DoubleVar(value=0.5)
        self.speed_z  = tk.DoubleVar(value=0.1)
        self.speed_ab = tk.DoubleVar(value=0.5)

        # Readout poll
        self._rot_after_id: str | None = None

        # Thread-safe queues drained by the Tk thread.
        self._log_q: queue.Queue[str] = queue.Queue()
        self._ui_task_q: queue.Queue = queue.Queue()
        self._log_after_id: str | None = self.after(100, self._drain_log_queue)

        # Wafer scan
        self.scan_folder_var   = tk.StringVar(value=os.path.join(os.getcwd(), "images"))
        self.scan_running      = False
        self._scan_start_pending = False
        self.scan_thread: threading.Thread | None = None
        self.scan_size_mm_var  = tk.DoubleVar(value=0.3)
        self.scan_n_var        = tk.IntVar(value=0)
        self.scan_settle_s_var = tk.DoubleVar(value=0.9)
        self.scan_start_delay_s_var = tk.DoubleVar(value=1.0)
        self.scan_capture_delay_s_var = tk.DoubleVar(value=0.1)
        self.scan_overwrite_var = tk.BooleanVar(value=True)
        self.snap_hotkey_var   = tk.StringVar(value="ToupCam SDK")
        self._snap_hotkey_handle = None
        self._manual_snap_save_lock = threading.Lock()
        self._pending_snap_request_lock = threading.Lock()
        self._pending_snap_request = None
        self._sdk_lock = threading.Lock()
        self._sdk_toupcam = None
        self._sdk_camera = None
        self._sdk_live_buf = None
        self._sdk_width = 0
        self._sdk_height = 0
        self._sdk_row_pitch = 0
        self._sdk_frame_counter = 0
        self._camera_capture_sequence = 0
        self._camera_publish_interval_s = 1.0 / 30.0
        self._camera_last_publish_monotonic = 0.0
        self._sdk_frame_event = threading.Event()
        self._sdk_disconnected = False
        self._sdk_latest_frame_bgr_cache = None
        self._sdk_device_name = ""
        self._sdk_preview_index = -1
        self._sdk_resolution_choices: list[tuple[int, int, int]] = []
        self._sdk_requested_resolution_index = SDK_PREVIEW_INDEX
        self._sdk_is_mono = False
        self._camera_ui_needs_sync = True
        self._camera_exposure_needs_sync = False
        self._camera_open_lock = threading.Lock()
        self._camera_open_future = None
        self._camera_close_future = None
        self._camera_preview_requested = False
        self._camera_pipeline_shutdown = False
        self._camera_lifecycle_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="camera-lifecycle"
        )
        self._camera_writer_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="camera-writer"
        )
        self._camera_preview_worker = LatestItemWorker(
            self._prepare_camera_preview,
            name="camera-preview-processor",
        )
        self._camera_preview_after_id: str | None = None
        self._camera_preview_photo = None
        self._camera_preview_last_frame_count = 0
        self._camera_preview_last_submitted_count = 0
        self._camera_preview_last_result_count = 0
        self.camera_status_var = tk.StringVar(
            value="Microscope preview idle. Click Start Live to open the camera."
        )
        self.camera_resolution_var = tk.StringVar(value="")
        self.camera_auto_exposure_var = tk.BooleanVar(value=False)
        self._camera_resolution_index_by_label: dict[str, int] = {}
        self._camera_control_vars = {
            "brightness": tk.DoubleVar(value=SDK_DEFAULT_BRIGHTNESS),
            "contrast": tk.DoubleVar(value=SDK_DEFAULT_CONTRAST),
            "saturation": tk.DoubleVar(value=SDK_DEFAULT_SATURATION),
            "gamma": tk.DoubleVar(value=SDK_DEFAULT_GAMMA),
            "hue": tk.DoubleVar(value=SDK_DEFAULT_HUE),
            "exposure_us": tk.DoubleVar(value=SDK_DEFAULT_EXPOSURE_US),
        }
        self._camera_control_value_vars = {
            key: tk.StringVar(value=self._format_camera_control_value(key, var.get()))
            for key, var in self._camera_control_vars.items()
        }
        self._camera_exposure_range_us = (SDK_EXPOSURE_MIN_US, SDK_EXPOSURE_MAX_US)
        self._camera_slider_widgets: dict[str, ttk.Scale] = {}
        self._camera_slider_after_ids: dict[str, str] = {}
        self._camera_color_presets: dict[str, dict[str, float]] = self._load_camera_color_presets()
        self.camera_preset_var = tk.StringVar(value="")
        self.camera_preset_combo: ttk.Combobox | None = None
        self.scan_corner_vars: dict[str, dict[str, tk.StringVar]] = {
            key: {
                "A": tk.StringVar(value=""),
                "B": tk.StringVar(value=""),
                "Z": tk.StringVar(value=""),
            }
            for _, key in SCAN_CORNER_ORDER
        }

        # Rotation / AB vars
        self.c0_var = tk.StringVar(value="")
        self.ct_var = tk.StringVar(value="")
        self.dc_var = tk.StringVar(value="")
        self.a_target_var = tk.StringVar(value="")
        self.b_target_var = tk.StringVar(value="")
        self.a_pos_var = tk.StringVar(value="--")
        self.b_pos_var = tk.StringVar(value="--")
        self.c0_var.trace_add("write", lambda *_: self._update_dc_display())
        self.ct_var.trace_add("write", lambda *_: self._update_dc_display())

        # Keyboard axis mapping  (swapped A arrows as per original)
        self._KB_MAP = {
            # keysym   : (axis_key, sign)
            "a": ("X", -1), "A": ("X", -1),
            "d": ("X", +1), "D": ("X", +1),
            "w": ("Y", +1), "W": ("Y", +1),
            "s": ("Y", -1), "S": ("Y", -1),
            "q": ("Z", -1), "Q": ("Z", -1),
            "e": ("Z", +1), "E": ("Z", +1),
            "Left":      ("A", +1),   
            "Right":     ("A", -1),  
            "Up":        ("B", +1),
            "Down":      ("B", -1),
            "Prior":     ("C", +1),   # Page Up
            "Next":      ("C", -1),   # Page Down
        }

        self._build_ui()

        # Stop all axes when keyboard lock is disabled
        self.kb_enabled.trace_add("write", self._on_lock_changed)

    # ── Layout ───────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.columnconfigure(0, weight=7, uniform="main")
        self.columnconfigure(1, weight=3, uniform="main")
        self.rowconfigure(0, weight=1)

        camera_shell = ttk.Frame(self, padding=(8, 8, 4, 8))
        camera_shell.grid(row=0, column=0, sticky="nsew")
        camera_shell.columnconfigure(0, weight=1)
        camera_shell.rowconfigure(0, weight=1)
        self._build_microscope_panel(camera_shell)

        controls_shell = ttk.Frame(self, padding=(4, 8, 8, 8))
        controls_shell.grid(row=0, column=1, sticky="nsew")
        controls_shell.columnconfigure(0, weight=1)
        controls_shell.rowconfigure(0, weight=1)

        controls_outer = ttk.LabelFrame(
            controls_shell,
            text="Controls",
            padding=0,
        )
        controls_outer.grid(row=0, column=0, sticky="nsew")
        controls_outer.columnconfigure(0, weight=1)
        controls_outer.rowconfigure(0, weight=1)

        controls_canvas = tk.Canvas(controls_outer, highlightthickness=0, borderwidth=0)
        controls_canvas.grid(row=0, column=0, sticky="nsew")
        controls_scroll = ttk.Scrollbar(
            controls_outer,
            orient="vertical",
            command=controls_canvas.yview,
        )
        controls_scroll.grid(row=0, column=1, sticky="ns")
        controls_canvas.configure(yscrollcommand=controls_scroll.set)

        self.controls_body = ttk.Frame(controls_canvas, padding=8)
        controls_window = controls_canvas.create_window(
            (0, 0),
            window=self.controls_body,
            anchor="nw",
        )

        def _sync_controls_scroll(_event=None):
            controls_canvas.configure(scrollregion=controls_canvas.bbox("all"))

        def _sync_controls_width(event):
            controls_canvas.itemconfigure(controls_window, width=event.width)

        self.controls_body.bind("<Configure>", _sync_controls_scroll)
        controls_canvas.bind("<Configure>", _sync_controls_width)
        self._build_controls_sidebar(self.controls_body)

        self.bind_all("<KeyPress>", self._on_keypress)
        self.bind_all("<KeyRelease>", self._on_keyrelease)
        self.bind_all("<FocusOut>", self._on_focus_out)
        self.bind_all("<Escape>", self._on_escape)
        try:
            self.winfo_toplevel().bind("<Unmap>", lambda e: self._on_focus_out(e))
        except Exception:
            pass
        self.focus_set()

        self.log("Keyboard map:  A/D=X  W/S=Y  Q/E=Z  â†/â†’=A  â†‘/â†“=B  PgUp/PgDn=C")
        self.log("Release key â†’ immediate hard stop.")
        self.after(300, self._auto_start_camera_preview)
        self.after(400, self._auto_connect)
        return

    def _build_rotation_panel(self, parent):
        rot = ttk.LabelFrame(parent, text="Rotation (C axis)", padding=8)
        rot.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        rot.columnconfigure(1, weight=1)

        rows = [
            ("C initial (C0):", self.c0_var, False),
            ("C current (Ct):", self.ct_var, False),
            ("dC = Ct − C0:",   self.dc_var, True),
        ]
        for i, (lbl, var, ro) in enumerate(rows):
            ttk.Label(rot, text=lbl).grid(row=i, column=0, sticky="e",
                                          padx=(0, 6), pady=2)
            kw = {"state": "readonly"} if ro else {}
            ttk.Entry(rot, textvariable=var, width=18, **kw).grid(
                row=i, column=1, sticky="w", pady=2)

        btns = ttk.Frame(rot)
        btns.grid(row=0, column=2, rowspan=3, sticky="ns", padx=(14, 0))
        ttk.Button(btns, text="Set C0 = Ct", command=self._set_c0_from_ct).pack(
            fill="x", pady=2)
        ttk.Button(btns, text="Move to Ct",  command=self._move_c_to_ct).pack(
            fill="x", pady=2)

        ttk.Label(rot, text="Live readout:").grid(
            row=3, column=0, sticky="w", pady=(8, 2))
        self.c_readout = tk.Text(rot, height=3, wrap="word", state="disabled")
        self.c_readout.grid(row=4, column=0, columnspan=3, sticky="we")

    def _build_ab_panel(self, parent):
        ab = ttk.LabelFrame(parent, text="Position (A / B)", padding=8)
        ab.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        ab.columnconfigure(1, weight=1)

        for i, (lbl, tgt_var, move_cmd) in enumerate([
            ("A target:", self.a_target_var, self._move_a),
            ("B target:", self.b_target_var, self._move_b),
        ]):
            ttk.Label(ab, text=lbl).grid(row=i, column=0, sticky="e",
                                         padx=(0, 6), pady=2)
            ttk.Entry(ab, textvariable=tgt_var, width=18).grid(
                row=i, column=1, sticky="w", pady=2)
            ttk.Button(ab, text=f"Move {lbl[0]}", command=move_cmd).grid(
                row=i, column=2, padx=8, pady=2)

        ttk.Label(ab, text="Live readout:").grid(
            row=2, column=0, sticky="w", pady=(8, 2))
        self.ab_readout = tk.Text(ab, height=3, wrap="word", state="disabled")
        self.ab_readout.grid(row=3, column=0, columnspan=3, sticky="we")

    def _build_scan_panel(self):
        sf = ttk.LabelFrame(self, text="Wafer Scan — Live Edge Detection", padding=8)
        sf.pack(fill=tk.X, padx=8, pady=4)
        sf.columnconfigure(1, weight=1)

        # Row 0 — save folder
        ttk.Label(sf, text="Save folder:").grid(row=0, column=0, sticky="e",
                                                 padx=(0, 6))
        ttk.Entry(sf, textvariable=self.scan_folder_var, width=52).grid(
            row=0, column=1, sticky="we")
        ttk.Button(sf, text="Browse…", command=self._browse_scan_folder).grid(
            row=0, column=2, padx=6)

        # Row 1 — tile step + grid limit
        ttk.Label(sf, text="Tile step:").grid(row=1, column=0, sticky="e",
                                               padx=(0, 6), pady=(6, 0))
        ttk.Entry(sf, textvariable=self.scan_size_mm_var, width=10).grid(
            row=1, column=1, sticky="w", pady=(6, 0))

        ttk.Label(sf, text="Max rows:").grid(row=1, column=1, sticky="e",
                                             padx=(0, 110), pady=(6, 0))
        ttk.Entry(sf, textvariable=self.scan_n_var, width=8).grid(
            row=1, column=2, sticky="w", pady=(6, 0))

        # Row 2 — settle + snap hotkey
        ttk.Label(sf, text="Settle (s):").grid(row=2, column=0, sticky="e",
                                                padx=(0, 6), pady=(6, 0))
        ttk.Entry(sf, textvariable=self.scan_settle_s_var, width=10).grid(
            row=2, column=1, sticky="w", pady=(6, 0))

        ttk.Label(sf, text="Snap hotkey:").grid(row=2, column=1, sticky="e",
                                                 padx=(0, 110), pady=(6, 0))
        ttk.Entry(sf, textvariable=self.snap_hotkey_var, width=8).grid(
            row=2, column=2, sticky="w", pady=(6, 0))

        # Row 3 — buttons + overwrite
        btns = ttk.Frame(sf)
        btns.grid(row=3, column=0, columnspan=3, sticky="we", pady=(10, 0))
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)
        ttk.Button(btns, text="▶  Start Scan", command=self.start_wafer_scan).grid(
            row=0, column=0, sticky="we", padx=(0, 4))
        ttk.Button(btns, text="■  Stop Scan",  command=self.stop_wafer_scan).grid(
            row=0, column=1, sticky="we", padx=(4, 0))

        ttk.Checkbutton(sf, text="Overwrite existing images",
                        variable=self.scan_overwrite_var).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

    # ── Utilities ────────────────────────────────────────────────────────────

    def _build_corner_scan_panel(self):
        sf = ttk.LabelFrame(self, text="Wafer Scan - Corner Guided", padding=8)
        sf.pack(fill=tk.X, padx=8, pady=4)
        sf.columnconfigure(1, weight=1)

        ttk.Label(sf, text="Save folder:").grid(
            row=0, column=0, sticky="e", padx=(0, 6))
        ttk.Entry(sf, textvariable=self.scan_folder_var, width=52).grid(
            row=0, column=1, sticky="we")
        ttk.Button(sf, text="Browse...", command=self._browse_scan_folder).grid(
            row=0, column=2, padx=6)

        ttk.Label(sf, text="Tile step:").grid(
            row=1, column=0, sticky="e", padx=(0, 6), pady=(6, 0))
        ttk.Entry(sf, textvariable=self.scan_size_mm_var, width=10).grid(
            row=1, column=1, sticky="w", pady=(6, 0))

        ttk.Label(sf, text="Max rows (0=auto):").grid(
            row=1, column=1, sticky="e", padx=(0, 110), pady=(6, 0))
        ttk.Entry(sf, textvariable=self.scan_n_var, width=8).grid(
            row=1, column=2, sticky="w", pady=(6, 0))

        ttk.Label(sf, text="Settle (s):").grid(
            row=2, column=0, sticky="e", padx=(0, 6), pady=(6, 0))
        ttk.Entry(sf, textvariable=self.scan_settle_s_var, width=10).grid(
            row=2, column=1, sticky="w", pady=(6, 0))

        ttk.Label(sf, text="Camera backend:").grid(
            row=2, column=1, sticky="e", padx=(0, 110), pady=(6, 0))
        ttk.Entry(sf, textvariable=self.snap_hotkey_var, width=14, state="readonly").grid(
            row=2, column=2, sticky="w", pady=(6, 0))

        ttk.Label(sf, text="Start delay (s):").grid(
            row=3, column=0, sticky="e", padx=(0, 6), pady=(6, 0))
        ttk.Entry(sf, textvariable=self.scan_start_delay_s_var, width=10).grid(
            row=3, column=1, sticky="w", pady=(6, 0))

        ttk.Label(sf, text="Capture delay (s):").grid(
            row=3, column=1, sticky="e", padx=(0, 110), pady=(6, 0))
        ttk.Entry(sf, textvariable=self.scan_capture_delay_s_var, width=8).grid(
            row=3, column=2, sticky="w", pady=(6, 0))

        ttk.Label(
            sf,
            text="Move the stage to each wafer corner and record the current A/B values. "
                 "Each scan step captures directly from the ToupCam SDK and saves into the selected folder.",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 4))

        cf = ttk.LabelFrame(sf, text="Recorded Corners", padding=6)
        cf.grid(row=5, column=0, columnspan=3, sticky="we")
        cf.columnconfigure(2, weight=1)
        cf.columnconfigure(4, weight=1)
        cf.columnconfigure(6, weight=1)

        ttk.Label(cf, text="Corner").grid(row=0, column=0, sticky="w")
        ttk.Label(cf, text="A").grid(row=0, column=2, sticky="w")
        ttk.Label(cf, text="B").grid(row=0, column=4, sticky="w")
        ttk.Label(cf, text="Focus Z").grid(row=0, column=6, sticky="w")

        for row, (label, key) in enumerate(SCAN_CORNER_ORDER, start=1):
            ttk.Label(cf, text=label + ":").grid(row=row, column=0, sticky="e", pady=2)
            ttk.Button(
                cf,
                text="Record Current",
                command=lambda k=key: self._record_scan_corner(k),
            ).grid(row=row, column=1, sticky="w", padx=(0, 8), pady=2)
            ttk.Entry(cf, textvariable=self.scan_corner_vars[key]["A"], width=14).grid(
                row=row, column=2, sticky="we", padx=(0, 8), pady=2)
            ttk.Entry(cf, textvariable=self.scan_corner_vars[key]["B"], width=14).grid(
                row=row, column=4, sticky="we", padx=(0, 8), pady=2)
            ttk.Entry(cf, textvariable=self.scan_corner_vars[key]["Z"], width=14).grid(
                row=row, column=6, sticky="we", pady=2)

        btns = ttk.Frame(sf)
        btns.grid(row=6, column=0, columnspan=3, sticky="we", pady=(10, 0))
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)
        btns.columnconfigure(2, weight=1)
        ttk.Button(btns, text="Start Scan",
                   style="Success.TButton",
                   command=self.start_corner_guided_scan).grid(
            row=0, column=0, sticky="we", padx=(0, 4))
        ttk.Button(btns, text="Stop Scan",
                   style="Danger.TButton",
                   command=self.stop_wafer_scan).grid(
            row=0, column=1, sticky="we", padx=(4, 0))
        ttk.Button(btns, text="Snap + Save Once",
                   command=self.snap_and_save_once).grid(
            row=0, column=2, sticky="we", padx=(4, 0))

        ttk.Checkbutton(
            sf,
            text="Overwrite existing images",
            variable=self.scan_overwrite_var,
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def _build_controls_sidebar(self, parent):
        if self.connections_in_header:
            self._build_shared_workflow_sidebar(parent)
            return

        conn = ttk.LabelFrame(parent, text="Connection", padding=8)
        conn.pack(fill=tk.X, padx=0, pady=(0, 4))

        ttk.Label(conn, text="DLL:").grid(row=0, column=0, sticky="e")
        ttk.Entry(conn, textvariable=self.var_dll, width=60).grid(
            row=0, column=1, columnspan=3, sticky="we", padx=4)
        ttk.Button(conn, text="Browse...", command=self._browse_dll).grid(
            row=0, column=4, padx=4)

        ttk.Label(conn, text="Port:").grid(row=1, column=0, sticky="e", pady=(6, 0))
        ttk.Entry(conn, textvariable=self.var_port, width=10).grid(
            row=1, column=1, sticky="w", padx=4, pady=(6, 0))
        ttk.Button(conn, text="Load DLL", command=self._load_dll).grid(
            row=1, column=2, padx=4, pady=(6, 0))
        ttk.Button(conn, text="Connect",
                   style="Success.TButton",
                   command=self._connect).grid(
            row=1, column=3, padx=4, pady=(6, 0))
        ttk.Button(conn, text="Disconnect",
                   style="Danger.TButton",
                   command=self._disconnect).grid(
            row=1, column=4, padx=4, pady=(6, 0))
        conn.columnconfigure(1, weight=1)

        kb_row = ttk.Frame(conn)
        kb_row.grid(row=2, column=0, columnspan=5, sticky="w", pady=(10, 0))
        ttk.Checkbutton(
            kb_row,
            text="Enable Keyboard Control",
            variable=self.kb_enabled,
        ).pack(side=tk.LEFT)
        self._lbl_lock = ttk.Label(kb_row, text="LOCKED",
                                   foreground=APP_DANGER,
                                   font=("Segoe UI", 9, "bold"))
        self._lbl_lock.pack(side=tk.LEFT, padx=(12, 0))
        self.kb_enabled.trace_add("write", self._refresh_lock_label)
        self._refresh_lock_label()

        spd = ttk.LabelFrame(parent, text="Speeds", padding=8)
        spd.pack(fill=tk.X, padx=0, pady=4)
        spd.columnconfigure(1, weight=1)

        def _speed_row(r, label, var, lo, hi):
            ttk.Label(spd, text=label).grid(row=r, column=0, sticky="e", padx=(0, 6))
            ttk.Scale(
                spd,
                from_=lo,
                to=hi,
                variable=var,
                orient="horizontal",
                length=280,
            ).grid(row=r, column=1, sticky="we")
            lbl = ttk.Label(spd, width=8)
            lbl.grid(row=r, column=2, sticky="w", padx=(8, 0))

            def _upd(*_):
                try:
                    lbl.config(text=f"{float(var.get()):.4f}")
                except Exception:
                    lbl.config(text="--")

            var.trace_add("write", _upd)
            _upd()

        def _log_speed_row(r, label, var, lo, hi, jog_axis_key=None, jog_vel=3.0):
            # Slider position is linear 0..1; the actual speed (in `var`) is
            # mapped exponentially so slow speeds get fine steps near the
            # bottom of the travel and fast speeds get coarse jumps near the top.
            ttk.Label(spd, text=label).grid(row=r, column=0, sticky="e", padx=(0, 6))

            pos_var = tk.DoubleVar()
            log_lo, log_hi = math.log(lo), math.log(hi)

            def _pos_to_speed(pos):
                return math.exp(log_lo + pos * (log_hi - log_lo))

            def _speed_to_pos(speed):
                speed = min(max(speed, lo), hi)
                return (math.log(speed) - log_lo) / (log_hi - log_lo)

            pos_var.set(_speed_to_pos(float(var.get())))

            ttk.Scale(
                spd,
                from_=0.0,
                to=1.0,
                variable=pos_var,
                orient="horizontal",
                length=280,
            ).grid(row=r, column=1, sticky="we")
            lbl = ttk.Label(spd, width=8)
            lbl.grid(row=r, column=2, sticky="w", padx=(8, 0))

            def _upd(*_):
                try:
                    speed = _pos_to_speed(float(pos_var.get()))
                    var.set(speed)
                    lbl.config(text=f"{speed:.4f}")
                except Exception:
                    lbl.config(text="--")

            pos_var.trace_add("write", _upd)
            _upd()

            # Up/down buttons: jog the axis directly at a fixed fast speed
            # while held down, and stop the instant the button is released.
            # The slider above is untouched by these.
            if jog_axis_key is not None:
                axis = AXIS_INDEX[jog_axis_key]

                def _press(vel):
                    self._start_manual_jog(axis, vel)

                def _release(_event=None):
                    self._stop_timed_loop(axis)

                step_col = ttk.Frame(spd)
                step_col.grid(row=r, column=3, sticky="w", padx=(4, 0))
                up_btn = ttk.Button(step_col, text="▲", width=2)
                up_btn.pack(side=tk.TOP)
                up_btn.bind("<ButtonPress-1>", lambda _e: _press(+jog_vel))
                up_btn.bind("<ButtonRelease-1>", _release)
                down_btn = ttk.Button(step_col, text="▼", width=2)
                down_btn.pack(side=tk.TOP)
                down_btn.bind("<ButtonPress-1>", lambda _e: _press(-jog_vel))
                down_btn.bind("<ButtonRelease-1>", _release)

        _speed_row(0, "XY speed:", self.speed_xy, 0.001, 1.0)
        _log_speed_row(1, "Z speed:", self.speed_z, 0.001, 1.0, jog_axis_key="Z")
        _speed_row(2, "AB speed:", self.speed_ab, 0.001, 1.0)

        btn_row = ttk.Frame(spd)
        btn_row.grid(row=3, column=0, columnspan=3, sticky="we", pady=(8, 0))
        for i in range(3):
            btn_row.columnconfigure(i, weight=1)
        ttk.Button(btn_row, text="Apply XY", command=self._apply_speed_xy).grid(
            row=0, column=0, sticky="we", padx=4)
        ttk.Button(btn_row, text="Apply Z", command=self._apply_speed_z).grid(
            row=0, column=1, sticky="we", padx=4)
        ttk.Button(btn_row, text="Apply AB (+C)", command=self._apply_speed_ab).grid(
            row=0, column=2, sticky="we", padx=4)

        mid = ttk.Frame(parent)
        mid.pack(fill=tk.X, padx=0, pady=4)
        mid.columnconfigure(0, weight=1)
        mid.columnconfigure(1, weight=1)
        self._build_rotation_panel(mid)
        self._build_ab_panel(mid)
        self._build_corner_scan_sidebar(parent)

        log_f = ttk.LabelFrame(parent, text="Status / Log", padding=6)
        log_f.pack(fill=tk.BOTH, expand=False, padx=0, pady=(4, 0))
        self.txt = tk.Text(
            log_f, height=10, wrap="word",
            bg="#0D1117", fg="#E6EDF3",
            insertbackground="#E6EDF3",
            selectbackground=APP_NAVY, selectforeground=APP_HDR_FG,
            font=("Consolas", 8), relief="flat", borderwidth=0,
            padx=6, pady=4,
        )
        sb = ttk.Scrollbar(log_f, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_shared_workflow_sidebar(self, parent):
        """Shared motion controls above lightweight Scan / Stack workflow tabs."""
        shared = ttk.Frame(parent)
        shared.pack(fill=tk.X, padx=0, pady=(0, 6))

        self._build_shared_axis_speeds(shared)

        self.micro_panel = MicroscopeControlPanel(shared, show_connections=False)
        self.micro_panel.pack(fill=tk.X, pady=(4, 0))
        self.micro_panel.external_log = self.tlog
        self.micro_panel.external_snap = self._autofocus_snap

        notebook = ttk.Notebook(parent)
        notebook.pack(fill=tk.BOTH, expand=True, padx=0, pady=(0, 4))

        scan_tab = ttk.Frame(notebook, padding=6)
        stack_tab = ttk.Frame(notebook, padding=6)
        notebook.add(scan_tab, text="  Scan  ")
        notebook.add(stack_tab, text="  Stack  ")

        self._build_corner_scan_sidebar(scan_tab, title="Scan Setup")

        self.temp_panel = CompactTemperaturePanel(stack_tab)
        self.temp_panel.external_log = self.tlog

        position_row = ttk.Frame(stack_tab)
        position_row.pack(fill=tk.X, pady=4)
        position_row.columnconfigure(0, weight=1)
        position_row.columnconfigure(1, weight=1)
        self._build_rotation_panel(position_row)
        self._build_ab_panel(position_row)

        notebook.select(stack_tab)

    def _build_shared_axis_speeds(self, parent):
        speeds = ttk.LabelFrame(parent, text="Speeds", padding=6)
        speeds.pack(fill=tk.X)
        speeds.columnconfigure(1, weight=1)

        rows = (
            ("XY", self.speed_xy, self._apply_speed_xy),
            ("Z", self.speed_z, self._apply_speed_z),
            ("A/B/C", self.speed_ab, self._apply_speed_ab),
        )
        for row, (label, variable, apply_command) in enumerate(rows):
            ttk.Label(speeds, text=f"{label}:", width=7, anchor="e").grid(
                row=row, column=0, sticky="e", padx=(0, 6), pady=2
            )
            ttk.Scale(
                speeds,
                from_=0.001,
                to=1.0,
                variable=variable,
                orient="horizontal",
            ).grid(row=row, column=1, sticky="we", pady=2)
            value_label = ttk.Label(speeds, width=7, anchor="e")
            value_label.grid(row=row, column=2, padx=(8, 4), pady=2)
            ttk.Button(
                speeds,
                text="Set",
                width=4,
                command=apply_command,
            ).grid(row=row, column=3, pady=2)

            if label == "Z":
                self._build_shared_z_jog_buttons(speeds, row)

            def update_value(*_args, var=variable, widget=value_label):
                try:
                    widget.config(text=f"{float(var.get()):.4f}")
                except Exception:
                    widget.config(text="--")

            variable.trace_add("write", update_value)
            update_value()

    def _build_shared_z_jog_buttons(self, parent, row):
        axis = AXIS_INDEX["Z"]

        def start(velocity):
            self._start_manual_jog(axis, velocity)

        def stop(_event=None):
            self._stop_timed_loop(axis)

        buttons = ttk.Frame(parent)
        buttons.grid(row=row, column=4, sticky="w", padx=(5, 0))
        up_button = ttk.Button(buttons, text="▲", width=2)
        down_button = ttk.Button(buttons, text="▼", width=2)
        up_button.pack(side=tk.TOP)
        down_button.pack(side=tk.TOP)
        up_button.bind("<ButtonPress-1>", lambda _event: start(+3.0))
        down_button.bind("<ButtonPress-1>", lambda _event: start(-3.0))
        up_button.bind("<ButtonRelease-1>", stop)
        down_button.bind("<ButtonRelease-1>", stop)

    def _build_corner_scan_sidebar(self, parent, title="Wafer Scan - Corner Guided"):
        sf = ttk.LabelFrame(parent, text=title, padding=8)
        sf.pack(fill=tk.X, padx=0, pady=4)
        sf.columnconfigure(1, weight=1)

        ttk.Label(sf, text="Save folder:").grid(
            row=0, column=0, sticky="e", padx=(0, 6))
        ttk.Entry(sf, textvariable=self.scan_folder_var, width=52).grid(
            row=0, column=1, sticky="we")
        ttk.Button(sf, text="Browse...", command=self._browse_scan_folder).grid(
            row=0, column=2, padx=6)

        ttk.Label(sf, text="Tile step:").grid(
            row=1, column=0, sticky="e", padx=(0, 6), pady=(6, 0))
        ttk.Entry(sf, textvariable=self.scan_size_mm_var, width=10).grid(
            row=1, column=1, sticky="w", pady=(6, 0))

        ttk.Label(sf, text="Max rows (0=auto):").grid(
            row=1, column=1, sticky="e", padx=(0, 110), pady=(6, 0))
        ttk.Entry(sf, textvariable=self.scan_n_var, width=8).grid(
            row=1, column=2, sticky="w", pady=(6, 0))

        ttk.Label(sf, text="Settle (s):").grid(
            row=2, column=0, sticky="e", padx=(0, 6), pady=(6, 0))
        ttk.Entry(sf, textvariable=self.scan_settle_s_var, width=10).grid(
            row=2, column=1, sticky="w", pady=(6, 0))

        ttk.Label(sf, text="Camera backend:").grid(
            row=2, column=1, sticky="e", padx=(0, 110), pady=(6, 0))
        ttk.Entry(sf, textvariable=self.snap_hotkey_var, width=14, state="readonly").grid(
            row=2, column=2, sticky="w", pady=(6, 0))

        ttk.Label(sf, text="Start delay (s):").grid(
            row=3, column=0, sticky="e", padx=(0, 6), pady=(6, 0))
        ttk.Entry(sf, textvariable=self.scan_start_delay_s_var, width=10).grid(
            row=3, column=1, sticky="w", pady=(6, 0))

        ttk.Label(sf, text="Capture delay (s):").grid(
            row=3, column=1, sticky="e", padx=(0, 110), pady=(6, 0))
        ttk.Entry(sf, textvariable=self.scan_capture_delay_s_var, width=8).grid(
            row=3, column=2, sticky="w", pady=(6, 0))

        ttk.Label(
            sf,
            text="Move the stage to each wafer corner and record the current A/B values. "
                 "Each scan step captures directly from the ToupCam SDK and saves into the selected folder.",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 4))

        cf = ttk.LabelFrame(sf, text="Recorded Corners", padding=6)
        cf.grid(row=5, column=0, columnspan=3, sticky="we")
        cf.columnconfigure(2, weight=1)
        cf.columnconfigure(4, weight=1)
        cf.columnconfigure(6, weight=1)

        ttk.Label(cf, text="Corner").grid(row=0, column=0, sticky="w")
        ttk.Label(cf, text="A").grid(row=0, column=2, sticky="w")
        ttk.Label(cf, text="B").grid(row=0, column=4, sticky="w")
        ttk.Label(cf, text="Focus Z").grid(row=0, column=6, sticky="w")

        for row, (label, key) in enumerate(SCAN_CORNER_ORDER, start=1):
            ttk.Label(cf, text=label + ":").grid(row=row, column=0, sticky="e", pady=2)
            ttk.Button(
                cf,
                text="Record Current",
                command=lambda k=key: self._record_scan_corner(k),
            ).grid(row=row, column=1, sticky="w", padx=(0, 8), pady=2)
            ttk.Entry(cf, textvariable=self.scan_corner_vars[key]["A"], width=14).grid(
                row=row, column=2, sticky="we", padx=(0, 8), pady=2)
            ttk.Entry(cf, textvariable=self.scan_corner_vars[key]["B"], width=14).grid(
                row=row, column=4, sticky="we", padx=(0, 8), pady=2)
            ttk.Entry(cf, textvariable=self.scan_corner_vars[key]["Z"], width=14).grid(
                row=row, column=6, sticky="we", pady=2)

        btns = ttk.Frame(sf)
        btns.grid(row=6, column=0, columnspan=3, sticky="we", pady=(10, 0))
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)
        btns.columnconfigure(2, weight=1)
        ttk.Button(btns, text="Start Scan",
                   style="Success.TButton",
                   command=self.start_corner_guided_scan).grid(
            row=0, column=0, sticky="we", padx=(0, 4))
        ttk.Button(btns, text="Stop Scan",
                   style="Danger.TButton",
                   command=self.stop_wafer_scan).grid(
            row=0, column=1, sticky="we", padx=(4, 0))
        ttk.Button(btns, text="Snap + Save Once",
                   command=self.snap_and_save_once).grid(
            row=0, column=2, sticky="we", padx=(4, 0))

        ttk.Checkbutton(
            sf,
            text="Overwrite existing images",
            variable=self.scan_overwrite_var,
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def _autofocus_snap(self):
        """Camera snapshot for autofocus: waits for a frame captured after this call, not a stale cached one."""
        self._ensure_sdk_camera_open()
        with self._sdk_lock:
            baseline = self._sdk_frame_counter
        try:
            self._wait_for_sdk_frame(baseline, SDK_FRAME_WAIT_TIMEOUT_S)
        except TimeoutError:
            self.tlog("[SDK] No fresh frame for autofocus snap; using latest available frame.")
        return self._sdk_latest_frame_bgr()

    def _build_microscope_panel(self, parent):
        panel = ttk.LabelFrame(parent, text="Microscope Live View", padding=10)
        panel.grid(row=0, column=0, sticky="nsew")
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(0, weight=1)

        preview_shell = ttk.Frame(panel)
        preview_shell.grid(row=0, column=0, sticky="nsew")
        preview_shell.columnconfigure(0, weight=1)
        preview_shell.rowconfigure(0, weight=1)

        self.camera_preview_label = tk.Label(
            preview_shell,
            text="Microscope preview\nClick  Start Live  to open the camera.",
            bg="#0D1117",
            fg="#4A8FCC",
            justify="center",
            font=("Segoe UI", 13),
        )
        self.camera_preview_label.grid(row=0, column=0, sticky="nsew")
        self.camera_preview_label.bind(
            "<Configure>",
            lambda _event: self._request_camera_preview_refresh(),
        )

        controls = ttk.Frame(panel)
        controls.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        controls.columnconfigure(0, weight=1, uniform="microscope_controls")
        controls.columnconfigure(1, weight=1, uniform="microscope_controls")

        if self.connections_in_header:
            log_f = ttk.LabelFrame(controls, text="Status / Log", padding=6)
            log_f.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
            self.txt = tk.Text(
                log_f,
                height=10,
                wrap="word",
                bg="#0D1117",
                fg="#E6EDF3",
                insertbackground="#E6EDF3",
                selectbackground=APP_NAVY,
                selectforeground=APP_HDR_FG,
                font=("Consolas", 8),
                relief="flat",
                borderwidth=0,
                padx=6,
                pady=4,
            )
            log_scroll = ttk.Scrollbar(log_f, orient="vertical", command=self.txt.yview)
            self.txt.configure(yscrollcommand=log_scroll.set)
            self.txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        else:
            self.micro_panel = MicroscopeControlPanel(controls)
            self.micro_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
            self.micro_panel.external_log = self.tlog
            self.micro_panel.external_snap = self._autofocus_snap

        look = ttk.LabelFrame(controls, text="Color Settings", padding=8)
        look.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        look.columnconfigure(0, weight=1, uniform="microscope_slider_cols")
        look.columnconfigure(1, weight=1, uniform="microscope_slider_cols")

        ttk.Checkbutton(
            look,
            text="Auto Exposure",
            variable=self.camera_auto_exposure_var,
            command=self._on_camera_auto_exposure_toggled,
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 6))

        self._build_microscope_slider(
            look, 1, 0, "Brightness", "brightness", SDK_BRIGHTNESS_MIN, SDK_BRIGHTNESS_MAX
        )
        self._build_microscope_slider(
            look, 1, 1, "Contrast", "contrast", SDK_CONTRAST_MIN, SDK_CONTRAST_MAX
        )
        self._build_microscope_slider(
            look, 2, 0, "Saturation", "saturation", SDK_SATURATION_MIN, SDK_SATURATION_MAX
        )
        self._build_microscope_slider(
            look, 2, 1, "Gamma", "gamma", SDK_GAMMA_MIN, SDK_GAMMA_MAX
        )
        self._build_microscope_slider(
            look, 3, 0, "Hue", "hue", SDK_HUE_MIN, SDK_HUE_MAX, columnspan=2
        )
        self._build_microscope_slider(
            look,
            4,
            0,
            "Exposure (ms)",
            "exposure_us",
            SDK_EXPOSURE_MIN_US,
            SDK_EXPOSURE_MAX_US,
            columnspan=2,
            value_width=12,
        )
        self._update_camera_exposure_controls()

        preset_row = ttk.Frame(look)
        preset_row.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        preset_row.columnconfigure(0, weight=1)

        self.camera_preset_combo = ttk.Combobox(
            preset_row,
            textvariable=self.camera_preset_var,
            state="readonly",
        )
        self.camera_preset_combo.grid(row=0, column=0, sticky="ew")

        preset_btns = ttk.Frame(preset_row)
        preset_btns.grid(row=0, column=1, sticky="e", padx=(6, 0))
        ttk.Button(
            preset_btns, text="Apply", command=self._apply_selected_camera_preset
        ).grid(row=0, column=0, padx=(0, 4))
        ttk.Button(
            preset_btns, text="Save As...", command=self._save_camera_color_preset
        ).grid(row=0, column=1, padx=(0, 4))
        ttk.Button(
            preset_btns, text="Delete", command=self._delete_selected_camera_preset
        ).grid(row=0, column=2)

        self._refresh_camera_preset_choices()

        ttk.Label(
            panel,
            textvariable=self.camera_status_var,
            wraplength=780,
            justify="left",
        ).grid(row=2, column=0, sticky="ew", pady=(10, 0))

    def _build_microscope_slider(self, parent, row, column, label, key, lo, hi, columnspan=1, value_width=6):
        cell = ttk.Frame(parent)
        cell.grid(
            row=row,
            column=column,
            columnspan=columnspan,
            sticky="ew",
            padx=4,
            pady=3,
        )
        cell.columnconfigure(0, weight=1)

        header = ttk.Frame(cell)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=label).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            textvariable=self._camera_control_value_vars[key],
            width=value_width,
        ).grid(row=0, column=1, sticky="e")

        scale = ttk.Scale(
            cell,
            from_=lo,
            to=hi,
            variable=self._camera_control_vars[key],
            orient="horizontal",
            command=lambda _value, k=key: self._on_camera_slider_changed(k),
        )
        scale.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self._camera_slider_widgets[key] = scale


    def log(self, msg: str):
        """Main-thread log."""
        try:
            self.txt.insert(tk.END, msg + "\n")
            self.txt.see(tk.END)
        except Exception:
            pass

    def tlog(self, msg: str):
        """Thread-safe log for worker threads."""
        try:
            self._log_q.put_nowait(msg)
        except Exception:
            pass

    def _post_ui_task(self, callback):
        """Queue a callback for execution by the Tk/main thread."""
        try:
            self._ui_task_q.put_nowait(callback)
        except Exception:
            pass

    def _watch_motion_future(self, future, on_success=None, error_title=None,
                             error_prefix="[Motion]"):
        """Deliver a motion-worker result back to Tk without blocking Tk."""
        def _done(completed):
            def _finish():
                try:
                    result = completed.result()
                except Exception as exc:
                    self.log(f"{error_prefix} error: {exc}")
                    if error_title:
                        messagebox.showerror(error_title, str(exc))
                    return
                if on_success is not None:
                    on_success(result)

            self._post_ui_task(_finish)

        future.add_done_callback(_done)
        return future

    def _drain_log_queue(self):
        try:
            while True:
                self._ui_task_q.get_nowait()()
        except queue.Empty:
            pass
        except Exception as exc:
            self.log(f"[UI task] ignored: {exc}")
        try:
            while True:
                self.log(self._log_q.get_nowait())
        except queue.Empty:
            pass
        except Exception:
            pass
        if self.winfo_exists():
            self._log_after_id = self.after(100, self._drain_log_queue)

    def _set_textbox(self, tb: tk.Text, text: str):
        try:
            tb.config(state="normal")
            tb.delete("1.0", "end")
            tb.insert("end", text)
            tb.config(state="disabled")
        except Exception:
            pass

    def _normalize_folder(self, p: str) -> str:
        p = (p or "").strip().strip('"')
        if not p:
            raise ValueError("Empty folder path.")
        if "://" in p:
            raise ValueError("Looks like a URL — use a local path.")
        p = os.path.abspath(os.path.expanduser(p))
        os.makedirs(p, exist_ok=True)
        return p

    # ── Browse dialogs ───────────────────────────────────────────────────────

    def _browse_dll(self):
        path = filedialog.askopenfilename(
            title="Select MCC6 DLL",
            filetypes=[("DLL", "*.dll"), ("All files", "*.*")],
        )
        if path:
            self.var_dll.set(path)

    def _browse_scan_folder(self):
        self.after(10, self._open_scan_folder_dialog)

    def _open_scan_folder_dialog(self):
        try:
            initial = (self.scan_folder_var.get() or "").strip().strip('"')
            if not initial or "://" in initial or not os.path.exists(initial):
                initial = os.path.expanduser("~")
            folder = filedialog.askdirectory(
                parent=self.winfo_toplevel(),
                initialdir=initial,
                title="Select save folder for wafer scan",
            )
            if folder:
                folder = self._normalize_folder(folder)
                self.scan_folder_var.set(folder)
                self.log(f"[UI] Scan folder → {folder}")
        except Exception as e:
            messagebox.showerror("Browse error", str(e))

    # ── Connection ───────────────────────────────────────────────────────────

    def _load_dll(self):
        path = self.var_dll.get().strip()
        future = self.controller.submit(
            "load_dll", path, priority=PRIORITY_INTERACTIVE
        )
        self._watch_motion_future(
            future,
            on_success=lambda _result: self.log(f"[DLL] Loaded: {path}"),
            error_title="DLL Load Error",
            error_prefix="[DLL]",
        )

    def _connect(self):
        path = self.var_dll.get().strip()
        port = self.var_port.get().strip()
        calls = []
        if not self.controller.dll_loaded:
            calls.append(("load_dll", (path,), {}))
        calls.append(("connect", (port,), {"logger": self.tlog}))
        future = self.controller.submit_batch(
            calls, priority=PRIORITY_INTERACTIVE
        )

        def _connected(results):
            ret = results[-1]
            self.log(f"[Connect] {port}  ret={ret}")
            self._apply_default_speeds()
            self._start_readout_loop()

        self._watch_motion_future(
            future,
            on_success=_connected,
            error_title="Connect Error",
            error_prefix="[Connect]",
        )

    def _disconnect(self):
        self._stop_readout_loop()
        self._pressed.clear()
        for axis in list(self._jog_after_id):
            self._jog_after_id.pop(axis, None)
        future = self.controller.stop_and_disconnect(
            AXIS_INDEX.values(), logger=self.tlog
        )

        def _disconnected(result):
            _disconnect_result, stop_errors = result
            for axis, error in stop_errors:
                self.log(f"[Disconnect] axis {axis} stop warning: {error}")
            self.log("[Disconnect] Done.")

        self._watch_motion_future(
            future,
            on_success=_disconnected,
            error_prefix="[Disconnect]",
        )

    def _auto_connect(self):
        """Try all hardware connections silently on startup using default settings."""
        dll_path = self.var_dll.get().strip()
        port = self.var_port.get().strip()
        focus_port = self.micro_panel.elev_port_var.get().strip()
        objective_port = self.micro_panel.obj_port_var.get().strip()

        def _worker():
            # 1. Load DLL from default path
            dll_ok = False
            try:
                if os.path.exists(dll_path):
                    self.controller.load_dll(dll_path)
                    self.tlog(f"[Auto] DLL loaded: {dll_path}")
                    dll_ok = True
                else:
                    self.tlog(f"[Auto] DLL not found at default path, skipping motion controller.")
            except Exception as e:
                self.tlog(f"[Auto] DLL load failed: {e}")

            # 2. Connect motion controller
            if dll_ok:
                try:
                    ret = self.controller.connect(port, logger=self.tlog)
                    self.tlog(f"[Auto] Motion controller connected on {port}  ret={ret}")
                    self._post_ui_task(self._apply_default_speeds)
                    self._post_ui_task(self._start_readout_loop)
                except Exception as e:
                    self.tlog(f"[Auto] Motion controller connect failed: {e}")

            # 3. Connect focus elevator
            focus_ok = self.micro_panel.connect_elevator(
                show_error=False,
                port=focus_port,
                # Startup clearance is deliberately open-loop. Avoid a
                # position query racing with the lift on controllers that only
                # acknowledge queries without returning position data.
                read_initial_position=False,
            )
            if focus_ok:
                self._post_ui_task(lambda: self.micro_panel._mark_port_connected(
                    self.micro_panel.focus_connect_btn))

            # 4. Connect objective nosepiece
            obj_ok = self.micro_panel.connect_nosepiece(
                show_error=False,
                initialize_default=True,
                port=objective_port,
                ui_dispatch=self._post_ui_task,
            )
            if obj_ok:
                self._post_ui_task(lambda: self.micro_panel._mark_port_connected(
                    self.micro_panel.obj_connect_btn))

        threading.Thread(target=_worker, daemon=True).start()

    # ── Speed helpers ────────────────────────────────────────────────────────

    def _apply_default_speeds(self):
        calls = []
        labels = []
        for k, v in DEFAULT_AXIS_SPEED.items():
            if k not in AXIS_INDEX:
                continue
            labels.append((k, v))
            calls.append((
                "send_param",
                (AXIS_INDEX[k], DEFAULT_SPEED_PARAM_INDEX, v),
                {},
            ))
        if not calls:
            return
        future = self.controller.submit_batch(calls, priority=PRIORITY_INTERACTIVE)

        def _applied(results):
            for (axis_key, value), result in zip(labels, results):
                self.log(f"[Speed] {axis_key} default {value} → ret={result}")

        self._watch_motion_future(
            future, on_success=_applied, error_prefix="[Speed defaults]"
        )

    def _apply_speed_xy(self):
        try:
            v = float(self.speed_xy.get())
        except Exception as e:
            messagebox.showerror("Apply XY Speed", str(e))
            return
        calls = [
            ("send_param", (AXIS_INDEX["X"], DEFAULT_SPEED_PARAM_INDEX, v), {}),
            ("send_param", (AXIS_INDEX["Y"], DEFAULT_SPEED_PARAM_INDEX, v), {}),
        ]
        future = self.controller.submit_batch(calls, priority=PRIORITY_INTERACTIVE)
        self._watch_motion_future(
            future,
            on_success=lambda results: self.log(
                f"[Speed] XY={v}  X:{results[0]}  Y:{results[1]}"
            ),
            error_title="Apply XY Speed",
            error_prefix="[Speed XY]",
        )

    def _apply_speed_z(self):
        try:
            v = float(self.speed_z.get())
        except Exception as e:
            messagebox.showerror("Apply Z Speed", str(e))
            return
        future = self.controller.submit(
            "send_param", AXIS_INDEX["Z"], DEFAULT_SPEED_PARAM_INDEX, v,
            priority=PRIORITY_INTERACTIVE,
        )
        self._watch_motion_future(
            future,
            on_success=lambda result: self.log(f"[Speed] Z={v}  ret:{result}"),
            error_title="Apply Z Speed",
            error_prefix="[Speed Z]",
        )

    def _apply_speed_ab(self):
        try:
            v = float(self.speed_ab.get())
        except Exception as e:
            messagebox.showerror("Apply AB Speed", str(e))
            return
        axis_keys = [key for key in ("A", "B", "C") if key in AXIS_INDEX]
        calls = [
            ("send_param", (AXIS_INDEX[key], DEFAULT_SPEED_PARAM_INDEX, v), {})
            for key in axis_keys
        ]
        future = self.controller.submit_batch(calls, priority=PRIORITY_INTERACTIVE)

        def _applied(results):
            result_text = "  ".join(
                f"{axis_key}:{result}"
                for axis_key, result in zip(axis_keys, results)
            )
            self.log(f"[Speed] AB={v}  {result_text}")

        self._watch_motion_future(
            future,
            on_success=_applied,
            error_title="Apply AB Speed",
            error_prefix="[Speed AB]",
        )

    # ── A / B absolute moves ─────────────────────────────────────────────────

    def _move_a(self):
        self._move_axis_to_target("A", self.a_target_var)

    def _move_b(self):
        self._move_axis_to_target("B", self.b_target_var)

    def _move_axis_to_target(self, axis_key: str, var: tk.StringVar):
        try:
            s = var.get().strip()
            if not s:
                raise ValueError(f"Enter {axis_key} target first.")
            target = float(s)
        except Exception as e:
            messagebox.showerror(f"Move {axis_key} Error", str(e))
            return
        future = self.controller.submit(
            "move_abs", AXIS_INDEX[axis_key], target,
            priority=PRIORITY_INTERACTIVE,
        )
        self._watch_motion_future(
            future,
            on_success=lambda result: self.log(
                f"[{axis_key}] Move abs {target:.6f} → ret={result}"
            ),
            error_title=f"Move {axis_key} Error",
            error_prefix=f"[{axis_key}]",
        )

    # ── Rotation (C) helpers ─────────────────────────────────────────────────

    def _set_c0_from_ct(self):
        try:
            ct_s = self.ct_var.get().strip()
            if not ct_s and "C" in AXIS_INDEX and self.controller.connected:
                sample = self.controller.position_snapshot().get(AXIS_INDEX["C"])
                if sample is None or sample.error is not None:
                    raise RuntimeError("No valid C position has been measured yet.")
                ct_s = f"{sample.value:.6f}"
                self.ct_var.set(ct_s)
            ct = float(ct_s)
            self.c0_var.set(f"{ct:.6f}")
            self.log(f"[C] C0 set to {ct:.6f}")
        except Exception as e:
            messagebox.showerror("Set C0 Error", str(e))

    def _update_dc_display(self):
        try:
            c0 = float(self.c0_var.get().strip())
            ct = float(self.ct_var.get().strip())
            self.dc_var.set(f"{ct - c0:.6f}")
        except Exception:
            self.dc_var.set("")

    def _move_c_to_ct(self):
        try:
            s = self.ct_var.get().strip()
            if not s:
                raise ValueError("Enter Ct target first.")
            target = float(s)
        except Exception as e:
            messagebox.showerror("Move C Error", str(e))
            return
        future = self.controller.submit(
            "move_abs", AXIS_INDEX["C"], target,
            priority=PRIORITY_INTERACTIVE,
        )
        self._watch_motion_future(
            future,
            on_success=lambda result: self.log(
                f"[C] Move abs {target:.6f} → ret={result}"
            ),
            error_title="Move C Error",
            error_prefix="[C]",
        )

    # ── Live readout poll ────────────────────────────────────────────────────

    def _start_readout_loop(self):
        if not self._rot_after_id:
            self._rot_after_id = self.after(200, self._poll_readouts)

    def _stop_readout_loop(self):
        aid = self._rot_after_id
        self._rot_after_id = None
        if aid:
            try:
                self.after_cancel(aid)
            except Exception:
                pass

    def _poll_readouts(self):
        self._rot_after_id = None
        try:
            if not self.winfo_exists():
                return
            if not self.controller.connected:
                self._set_textbox(self.c_readout,  "Ct: --\ndC: --\n")
                self._set_textbox(self.ab_readout, "A: --\nB: --\n")
                return

            # These are cached values; no controller/DLL call runs on Tk.
            snapshot = self.controller.position_snapshot()

            def _position(axis_key):
                sample = snapshot.get(AXIS_INDEX[axis_key])
                if sample is None or sample.error is not None:
                    return None
                if not math.isfinite(sample.value):
                    return None
                return sample.value

            # A / B
            pa = _position("A") if "A" in AXIS_INDEX else None
            pb = _position("B") if "B" in AXIS_INDEX else None
            a_txt = "A: --" if pa is None else f"A: {pa:.6f}"
            b_txt = "B: --" if pb is None else f"B: {pb:.6f}"
            if pa is not None:
                self.a_pos_var.set(f"{pa:.6f}")
            if pb is not None:
                self.b_pos_var.set(f"{pb:.6f}")
            self._set_textbox(self.ab_readout, f"{a_txt}\n{b_txt}\n")

            # C
            ct_txt = "Ct: --"
            dc_txt = "dC: --"
            if "C" in AXIS_INDEX:
                pc = _position("C")
            else:
                pc = None
            if pc is not None:
                ct_txt = f"Ct: {pc:.6f}"
                # Auto-update Ct entry only when user is not typing in an Entry
                try:
                    fw = self.winfo_toplevel().focus_get()
                    if not isinstance(fw, (tk.Entry, ttk.Entry)):
                        self.ct_var.set(f"{pc:.6f}")
                except Exception:
                    pass
                try:
                    c0 = float(self.c0_var.get().strip())
                    dc_txt = f"dC: {pc - c0:.6f}"
                    self.dc_var.set(f"{pc - c0:.6f}")
                except Exception:
                    dc_txt = "dC: --"
            self._set_textbox(self.c_readout, f"{ct_txt}\n{dc_txt}\n")

        except Exception:
            pass
        finally:
            if self.winfo_exists():
                self._rot_after_id = self.after(200, self._poll_readouts)

    # ── Keyboard lock label ───────────────────────────────────────────────────

    def _refresh_lock_label(self, *_):
        if self.kb_enabled.get():
            self._lbl_lock.config(text="UNLOCKED", foreground=APP_SUCCESS)
        else:
            self._lbl_lock.config(text="LOCKED",   foreground=APP_DANGER)

    def _on_lock_changed(self, *_):
        if not self.kb_enabled.get():
            self._stop_all_keyboard_axes()
            self._pressed.clear()

    # ── Keyboard event handlers ───────────────────────────────────────────────

    @staticmethod
    def _is_typing_widget(event) -> bool:
        return isinstance(getattr(event, "widget", None),
                          (tk.Entry, tk.Text, ttk.Entry))

    def _on_keypress(self, event):
        if not self.kb_enabled.get() or self._is_typing_widget(event):
            return
        if event.keysym not in self._KB_MAP or event.keysym in self._pressed:
            return
        self._pressed.add(event.keysym)
        axis_key, _ = self._KB_MAP[event.keysym]
        self._start_timed_jog(AXIS_INDEX[axis_key], axis_key)

    def _on_keyrelease(self, event):
        if event.keysym not in self._KB_MAP:
            return
        if event.keysym not in self._pressed:
            return
        self._pressed.discard(event.keysym)
        axis_key, _ = self._KB_MAP[event.keysym]
        axis = AXIS_INDEX[axis_key]
        self._stop_timed_loop(axis)
        if self._axis_still_commanded(axis_key):
            self._start_timed_jog(axis, axis_key)

    def _on_focus_out(self, event):
        # Moving focus between ordinary widgets (including Notebook tabs) must
        # not send six synchronous hardware stop commands. Stop only when a
        # keyboard/manual jog is actually active.
        if self._pressed or self._jog_after_id:
            self._stop_all_keyboard_axes()
            self._pressed.clear()

    def _on_escape(self, event):
        self._stop_all_keyboard_axes()
        self._pressed.clear()

    # ── Timed-jog helpers ────────────────────────────────────────────────────

    def _axis_speed(self, axis_key: str) -> float:
        try:
            if axis_key in ("X", "Y"):
                return float(self.speed_xy.get())
            if axis_key == "Z":
                return float(self.speed_z.get())
            if axis_key in ("A", "B", "C"):
                return float(self.speed_ab.get())
        except Exception:
            pass
        return 0.5

    def _desired_vel(self, axis_key: str) -> float:
        pos = neg = 0
        for ks in self._pressed:
            if ks not in self._KB_MAP:
                continue
            k, sign = self._KB_MAP[ks]
            if k != axis_key:
                continue
            if sign > 0:
                pos += 1
            else:
                neg += 1
        if pos and neg:
            return 0.0
        spd = self._axis_speed(axis_key)
        if pos:
            return +spd
        if neg:
            return -spd
        return 0.0

    def _axis_still_commanded(self, axis_key: str) -> bool:
        return any(
            self._KB_MAP.get(ks, (None,))[0] == axis_key
            for ks in self._pressed
        )

    def _start_timed_jog(self, axis: int, axis_key: str):
        if not self.kb_enabled.get() or not self._axis_still_commanded(axis_key):
            self._stop_timed_loop(axis)
            return
        velocity = self._desired_vel(axis_key)
        if velocity == 0.0:
            self._stop_timed_loop(axis)
            return
        self._jog_after_id[axis] = True
        future = self.controller.start_jog(
            axis, velocity, DEFAULT_JOG_ACCEL, self._jog_dt
        )
        self._watch_motion_future(future, error_prefix=f"[Jog {axis_key}]")

    def _start_manual_jog(self, axis: int, vel: float):
        """Continuous jog at a fixed velocity, driven by a held button rather
        than the keyboard-jog key-combo logic in `_start_timed_jog`."""
        self._jog_after_id[axis] = True
        future = self.controller.start_jog(
            axis, vel, DEFAULT_JOG_ACCEL, self._jog_dt
        )
        self._watch_motion_future(future, error_prefix=f"[Jog axis {axis}]")

    def _stop_timed_loop(self, axis: int):
        was_active = self._jog_after_id.pop(axis, None)
        if was_active is None:
            return
        future = self.controller.stop_jog(axis)
        self._watch_motion_future(future, error_prefix=f"[STOP axis {axis}]")

    def _stop_all_keyboard_axes(self):
        self._jog_after_id.clear()
        if not self.controller.connected:
            return
        future = self.controller.stop_all(AXIS_INDEX.values())

        def _stopped(stop_errors):
            for axis, error in stop_errors:
                self.log(f"[STOP all] axis {axis} warning: {error}")

        self._watch_motion_future(
            future, on_success=_stopped, error_prefix="[STOP all]"
        )


    def on_close(self):
        self.scan_running = False
        self._scan_start_pending = False

        handle = getattr(self, "_snap_hotkey_handle", None)
        if handle is not None:
            try:
                keyboard.remove_hotkey(handle)
            except Exception:
                pass
            self._snap_hotkey_handle = None

        for aid_attr in ("_rot_after_id", "_log_after_id"):
            aid = getattr(self, aid_attr, None)
            if aid:
                try:
                    self.after_cancel(aid)
                except Exception:
                    pass
            setattr(self, aid_attr, None)

        if self._camera_preview_after_id:
            try:
                self.after_cancel(self._camera_preview_after_id)
            except Exception:
                pass
            self._camera_preview_after_id = None

        for aid in list(self._camera_slider_after_ids.values()):
            try:
                self.after_cancel(aid)
            except Exception:
                pass
        self._camera_slider_after_ids.clear()

        self._stop_all_keyboard_axes()
        self._shutdown_camera_pipeline()
        try:
            self.micro_panel.on_close()
        except Exception:
            pass
        try:
            self.temp_panel.close()
        except Exception:
            pass
        # shut down server
        try:
            self.popup.shutdown()
        except Exception:
            pass

        # Complete the final stop/disconnect before terminating the sole DLL
        # owner. The timeout prevents window shutdown from hanging forever if
        # the vendor DLL stops responding.
        try:
            self.controller.stop_and_disconnect(
                AXIS_INDEX.values(), logger=self.tlog
            ).result(timeout=2.0)
        except Exception as exc:
            self.tlog(f"[Disconnect] close-time cleanup: {exc}")
        finally:
            self.controller.shutdown(wait=True, timeout=1.0)
