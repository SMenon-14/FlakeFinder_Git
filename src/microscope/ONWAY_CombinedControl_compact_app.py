#!/usr/bin/env python3
"""Compact, no-scroll entry point for the ONWAY combined control GUI.

This file intentionally leaves the existing combined-control entry point and
panels untouched.  It reuses all motion, camera, scan, and temperature logic,
but presents the tall controls sidebar as three fixed-height tabs.
"""

import os
import tkinter as tk
from tkinter import ttk

from src.microscope.onway_app_config import (
    APP_BG,
    APP_BORDER,
    APP_DANGER,
    APP_HDR_FG,
    APP_MUTED,
    APP_NAVY,
    APP_NAVY2,
    APP_PANEL,
    APP_SUCCESS,
    APP_TEXT,
    ICON_PATH,
    configure_app_style,
)
from src.microscope.onway_axis_control_panel import AxisControlPanel
from src.microscope.onway_microscope_panel import MicroscopeControlPanel
from src.microscope.onway_motion_hardware import AXIS_INDEX
from src.microscope.onway_temperature_panel import CompactTemperaturePanel, data_store


class CompactAxisControlPanel(AxisControlPanel):
    """Axis panel with tabbed controls and no outer scrolling canvas."""

    def _build_microscope_panel(self, parent):
        """Build the standard camera area, replacing its focus card with the log."""
        super()._build_microscope_panel(parent)

        old_micro_panel = self.micro_panel
        bottom_controls = old_micro_panel.master
        old_micro_panel.on_close()
        old_micro_panel.destroy()

        log_frame = ttk.LabelFrame(bottom_controls, text="Status / Log", padding=6)
        log_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.txt = tk.Text(
            log_frame,
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
            padx=7,
            pady=5,
        )
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=log_scroll.set)
        self.txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

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

        self.controls_body = ttk.Frame(controls_shell)
        self.controls_body.grid(row=0, column=0, sticky="nsew")
        self._build_controls_sidebar(self.controls_body)

        self.bind_all("<KeyPress>", self._on_keypress)
        self.bind_all("<KeyRelease>", self._on_keyrelease)
        self.bind_all("<FocusOut>", self._on_focus_out)
        self.bind_all("<Escape>", self._on_escape)
        try:
            self.winfo_toplevel().bind("<Unmap>", lambda event: self._on_focus_out(event))
        except Exception:
            pass
        self.focus_set()

        self.log("Keyboard: A/D=X  W/S=Y  Q/E=Z  arrows=A/B  PgUp/PgDn=C")
        self.log("Release any jog key for an immediate hard stop.")
        self.after(300, self._auto_start_camera_preview)
        self.after(400, self._auto_connect)

    def _build_controls_sidebar(self, parent):
        notebook = ttk.Notebook(parent)
        notebook.pack(fill=tk.BOTH, expand=True)

        motion_tab = ttk.Frame(notebook, padding=8)
        scan_tab = ttk.Frame(notebook, padding=8)
        notebook.add(motion_tab, text="  Motion  ")
        notebook.add(scan_tab, text="  Wafer Scan  ")

        self._build_compact_speeds(motion_tab)

        position_row = ttk.Frame(motion_tab)
        position_row.pack(fill=tk.X, pady=4)
        position_row.columnconfigure(0, weight=1)
        position_row.columnconfigure(1, weight=1)
        self._build_rotation_panel(position_row)
        self._build_ab_panel(position_row)

        lower_notebook = ttk.Notebook(motion_tab)
        lower_notebook.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        focus_tab = ttk.Frame(lower_notebook, padding=4)
        temperature_tab = ttk.Frame(lower_notebook, padding=4)
        lower_notebook.add(focus_tab, text="  Focus / Objective  ")
        lower_notebook.add(temperature_tab, text="  Temperature  ")

        self.micro_panel = MicroscopeControlPanel(focus_tab)
        self.micro_panel.pack(fill=tk.X)
        self.micro_panel.external_log = self.tlog
        self.micro_panel.external_snap = self._autofocus_snap

        self.temp_panel = CompactTemperaturePanel(temperature_tab)
        self.temp_panel.external_log = self.tlog

        self._build_corner_scan_sidebar(scan_tab)

        hint = ttk.Label(
            scan_tab,
            text="Tip: record all four corners before starting a scan.",
            foreground=APP_MUTED,
        )
        hint.pack(anchor="w", pady=(8, 0))

    def _build_compact_speeds(self, parent):
        speeds = ttk.LabelFrame(parent, text="Axis Speeds", padding=8)
        speeds.pack(fill=tk.X, pady=(0, 4))
        speeds.columnconfigure(1, weight=1)

        rows = (
            ("XY", self.speed_xy, self._apply_speed_xy),
            ("Z", self.speed_z, self._apply_speed_z),
            ("AB + C", self.speed_ab, self._apply_speed_ab),
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
                text="Apply",
                width=7,
                command=apply_command,
            ).grid(row=row, column=3, pady=2)

            if label == "Z":
                self._build_coarse_z_buttons(speeds, row)

            def update_value(*_args, var=variable, widget=value_label):
                try:
                    widget.config(text=f"{float(var.get()):.4f}")
                except Exception:
                    widget.config(text="--")

            variable.trace_add("write", update_value)
            update_value()

    def _build_coarse_z_buttons(self, parent, row):
        """Restore the press-and-hold coarse stage-Z controls."""
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


class CompactCombinedApp(tk.Tk):
    """Top-level compact application with connection controls in its header."""

    def __init__(self):
        super().__init__()
        configure_app_style()

        self.title("ONWAY Combined Control — Compact")
        self.configure(bg=APP_BG)
        try:
            self.state("zoomed")
        except Exception:
            self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")
        self.minsize(1280, 760)

        if ICON_PATH and os.path.exists(ICON_PATH):
            try:
                self.iconbitmap(ICON_PATH)
            except Exception:
                pass

        self.header = tk.Frame(self, bg=APP_NAVY, height=48)
        self.header.pack(fill=tk.X, side=tk.TOP)
        self.header.pack_propagate(False)

        tk.Label(
            self.header,
            text="ONWAY  Combined Control",
            bg=APP_NAVY,
            fg=APP_HDR_FG,
            font=("Segoe UI", 12, "bold"),
        ).pack(side=tk.LEFT, padx=(16, 20), fill=tk.Y)

        self.close_button = self._header_button(
            self.header, "Close", self.on_close, APP_DANGER, width=7
        )
        self.close_button.pack(side=tk.RIGHT, padx=(6, 14), pady=9)

        self.live_status = tk.Label(
            self.header,
            text="Starting…",
            bg=APP_NAVY,
            fg="#D6EAF8",
            font=("Segoe UI", 9),
        )
        self.live_status.pack(side=tk.RIGHT, padx=10, fill=tk.Y)

        content = ttk.Frame(self)
        content.pack(fill=tk.BOTH, expand=True)
        self.axis_panel = CompactAxisControlPanel(content)

        self._build_connection_header()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(500, self._refresh_header)

    @staticmethod
    def _header_button(parent, text, command, color=APP_NAVY2, width=None):
        return tk.Button(
            parent,
            text=text,
            command=command,
            width=width,
            bg=color,
            fg=APP_HDR_FG,
            activebackground=APP_NAVY2,
            activeforeground=APP_HDR_FG,
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            font=("Segoe UI", 9),
            padx=7,
        )

    def _build_connection_header(self):
        connection = tk.Frame(self.header, bg=APP_NAVY)
        connection.pack(side=tk.LEFT, fill=tk.Y)

        tk.Label(
            connection, text="DLL", bg=APP_NAVY, fg=APP_HDR_FG, font=("Segoe UI", 9)
        ).pack(side=tk.LEFT, padx=(0, 4))
        dll_entry = tk.Entry(
            connection,
            textvariable=self.axis_panel.var_dll,
            width=29,
            relief="flat",
            font=("Segoe UI", 9),
            bg=APP_PANEL,
            fg=APP_TEXT,
        )
        dll_entry.pack(side=tk.LEFT, ipady=3, pady=10)

        self._header_button(
            connection, "…", self.axis_panel._browse_dll, width=2
        ).pack(side=tk.LEFT, padx=3, pady=9)
        self._header_button(
            connection, "Load", self.axis_panel._load_dll, width=5
        ).pack(side=tk.LEFT, padx=(0, 10), pady=9)

        tk.Label(
            connection, text="Port", bg=APP_NAVY, fg=APP_HDR_FG, font=("Segoe UI", 9)
        ).pack(side=tk.LEFT, padx=(0, 4))
        tk.Entry(
            connection,
            textvariable=self.axis_panel.var_port,
            width=7,
            relief="flat",
            justify="center",
            font=("Segoe UI", 9),
            bg=APP_PANEL,
            fg=APP_TEXT,
        ).pack(side=tk.LEFT, ipady=3, pady=10)

        self._header_button(
            connection, "Connect", self.axis_panel._connect, APP_SUCCESS, width=8
        ).pack(side=tk.LEFT, padx=(6, 3), pady=9)
        self._header_button(
            connection, "Disconnect", self.axis_panel._disconnect, APP_DANGER, width=10
        ).pack(side=tk.LEFT, padx=(0, 10), pady=9)

        keyboard_toggle = tk.Checkbutton(
            connection,
            text="Keyboard",
            variable=self.axis_panel.kb_enabled,
            bg=APP_NAVY,
            fg=APP_HDR_FG,
            activebackground=APP_NAVY,
            activeforeground=APP_HDR_FG,
            selectcolor=APP_NAVY2,
            font=("Segoe UI", 9),
            borderwidth=0,
            highlightthickness=0,
        )
        keyboard_toggle.pack(side=tk.LEFT, padx=(0, 4), pady=8)

        self.axis_panel._lbl_lock = tk.Label(
            connection,
            text="LOCKED",
            bg=APP_NAVY,
            fg="#FFB3B3",
            font=("Segoe UI", 8, "bold"),
        )
        self.axis_panel._lbl_lock.pack(side=tk.LEFT, padx=(0, 6), fill=tk.Y)
        self.axis_panel.kb_enabled.trace_add("write", self._refresh_header_lock)
        self._refresh_header_lock()

    def _refresh_header_lock(self, *_args):
        enabled = self.axis_panel.kb_enabled.get()
        self.axis_panel._lbl_lock.config(
            text="UNLOCKED" if enabled else "LOCKED",
            fg="#A9DFBF" if enabled else "#FFB3B3",
        )

    def _refresh_header(self):
        try:
            if not self.winfo_exists():
                return
            connection = "Connected" if self.axis_panel.controller.connected else "Offline"
            parts = [connection]
            temperature = data_store.get("Temperature")
            setpoint = data_store.get("SetTemperature")
            power = data_store.get("Power")
            if temperature is not None:
                parts.append(f"T {temperature:.1f} °C")
            if setpoint is not None:
                parts.append(f"SP {setpoint:.1f} °C")
            if power is not None:
                parts.append(f"Power {power:.0f}%")
            self.live_status.config(text="  |  ".join(parts))
            self.after(500, self._refresh_header)
        except Exception:
            pass

    def on_close(self):
        try:
            self.axis_panel.on_close()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass


if __name__ == "__main__":
    application = CompactCombinedApp()
    try:
        application.mainloop()
    except KeyboardInterrupt:
        application.on_close()
