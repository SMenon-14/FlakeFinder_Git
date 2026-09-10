#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ONWAY Combined Control -- split-module entry point (Temperature header + AxisControlPanel)."""
import os
import sys
import subprocess
import importlib.util
import sysconfig
import tkinter as tk
from tkinter import ttk
from src.paths import MENU_DIR, APP_ROOT

from src.microscope.onway_app_config import (
    configure_app_style, APP_BG, APP_NAVY, APP_NAVY2, APP_DANGER,
    APP_HDR_FG, APP_BORDER, APP_PANEL, APP_SUCCESS, APP_TEXT, ICON_PATH,
)
from src.microscope.onway_temperature_panel import data_store
from src.microscope.onway_axis_control_panel import AxisControlPanel

def run_axis_control_standalone():
    root = tk.Tk()
    root.title("Axis Control")
    root.geometry("1100x820")
    panel = AxisControlPanel(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (panel.on_close(), root.destroy()))
    root.mainloop()

class CombinedApp(tk.Tk):
    """Main window — Navy header + full-screen AxisControlPanel."""

    def __init__(self):
        super().__init__()
        configure_app_style()

        self.title("ONWAY Combined Control")
        self.configure(bg=APP_BG)
        try:
            self.state("zoomed")
        except Exception:
            self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")
        self.minsize(1280, 820)

        if ICON_PATH and os.path.exists(ICON_PATH):
            try:
                self.iconbitmap(ICON_PATH)
            except Exception:
                pass

        # ── Navy header bar ───────────────────────────────────────────────────
        self.header = tk.Frame(self, bg=APP_NAVY, height=46)
        self.header.pack(fill="x", side=tk.TOP)
        self.header.pack_propagate(False)

        # 1. Left-aligned Title
        tk.Label(
            self.header,
            text="ONWAY Control",
            bg=APP_NAVY, fg=APP_HDR_FG,
            font=("Segoe UI", 12, "bold"),
        ).pack(side=tk.LEFT, padx=18, fill="y")

        # Nav button factory function
        def _nav_btn(text, fg_normal, command):
            lbl = tk.Label(
                self.header, text=text, font=("Segoe UI", 9),
                fg=fg_normal, bg=APP_NAVY, cursor="hand2",
            )
            lbl.bind("<Enter>",    lambda e: lbl.config(fg="#FFFFFF"))
            lbl.bind("<Leave>",    lambda e: lbl.config(fg=fg_normal))
            lbl.bind("<Button-1>", lambda e: command())
            return lbl

        # 2. PACK FIRST TO THE RIGHT: Home Button (Claims the absolute right edge)
        _nav_btn("✕  Close", APP_DANGER, self.on_close).pack(side=tk.RIGHT, padx=12, fill="y")

        # 3. PACK SECOND TO THE RIGHT: Live status (Stacks nicely to the left of Home)
        self._hdr_status = tk.Label(
            self.header, text="", bg=APP_NAVY, fg="#A9CCE3",
            font=("Segoe UI", 9),
        )
        self._hdr_status.pack(side=tk.RIGHT, padx=18, fill="y")


        # ── Main content ──────────────────────────────────────────────────────
        content = ttk.Frame(self)
        content.pack(fill="both", expand=True)
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)

        self.axis_panel = AxisControlPanel(content, connections_in_header=True)
        self._build_connection_header()

        # ── Slim footer ───────────────────────────────────────────────────────
        footer = tk.Frame(self, bg=APP_BORDER, height=22)
        footer.pack(fill="x", side=tk.BOTTOM)
        footer.pack_propagate(False)
        self._footer_lbl = tk.Label(
            footer, text="Ready", bg=APP_BORDER, fg=APP_TEXT,
            font=("Segoe UI", 8), anchor="w",
        )
        self._footer_lbl.pack(side=tk.LEFT, padx=10, fill="y")

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(600, self._tick_header)

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
            font=("Segoe UI", 8),
            padx=4,
        )

    def _build_connection_header(self):
        """Place motion, focus, and objective connections in the blue header."""
        connection = tk.Frame(self.header, bg=APP_NAVY)
        connection.pack(side=tk.LEFT, fill=tk.Y)

        def header_label(text):
            tk.Label(
                connection,
                text=text,
                bg=APP_NAVY,
                fg=APP_HDR_FG,
                font=("Segoe UI", 8),
            ).pack(side=tk.LEFT, padx=(5, 2))

        # Keep DLL actions available without spending a full entry-width on
        # the path, which is already stored in the controller variable.
        self._header_button(
            connection, "DLL…", self.axis_panel._browse_dll, width=4
        ).pack(side=tk.LEFT, padx=(5, 2), pady=8)
        self._header_button(
            connection, "Load", self.axis_panel._load_dll, width=4
        ).pack(side=tk.LEFT, padx=(0, 5), pady=8)

        header_label("Stage")
        tk.Entry(
            connection,
            textvariable=self.axis_panel.var_port,
            width=6,
            relief="flat",
            justify="center",
            font=("Segoe UI", 8),
            bg=APP_PANEL,
            fg=APP_TEXT,
        ).pack(side=tk.LEFT, ipady=2, pady=9)
        self._header_button(
            connection, "On", self.axis_panel._connect, APP_SUCCESS, width=3
        ).pack(side=tk.LEFT, padx=2, pady=8)
        self._header_button(
            connection, "Off", self.axis_panel._disconnect, APP_DANGER, width=3
        ).pack(side=tk.LEFT, padx=(0, 5), pady=8)

        # Reuse the focus/objective controller owned by the shared right panel.
        micro = self.axis_panel.micro_panel
        micro.connect_button_idle_text = "On"
        micro.connect_button_connected_text = "OK"
        micro.connect_button_idle_bg = APP_NAVY2
        micro.connect_button_connected_bg = APP_SUCCESS
        header_label("Focus")
        micro.elev_port_combo = ttk.Combobox(
            connection,
            textvariable=micro.elev_port_var,
            width=6,
            state="readonly",
        )
        micro.elev_port_combo.pack(side=tk.LEFT, pady=8)
        micro.focus_connect_btn = self._header_button(
            connection, "On", micro._connect_elevator_clicked, width=3
        )
        micro.focus_connect_btn.pack(side=tk.LEFT, padx=(2, 5), pady=8)

        header_label("Obj")
        micro.obj_port_combo = ttk.Combobox(
            connection,
            textvariable=micro.obj_port_var,
            width=6,
            state="readonly",
        )
        micro.obj_port_combo.pack(side=tk.LEFT, pady=8)
        micro.obj_connect_btn = self._header_button(
            connection, "On", micro._connect_nosepiece_clicked, width=3
        )
        micro.obj_connect_btn.pack(side=tk.LEFT, padx=2, pady=8)
        self._header_button(
            connection, "Ports", micro.refresh_ports, width=5
        ).pack(side=tk.LEFT, padx=2, pady=8)
        self._header_button(
            connection, "All Off", micro._disconnect_all_clicked, APP_DANGER, width=5
        ).pack(side=tk.LEFT, padx=(0, 5), pady=8)

        micro.refresh_ports()

        keyboard_toggle = tk.Checkbutton(
            connection,
            text="Keys",
            variable=self.axis_panel.kb_enabled,
            bg=APP_NAVY,
            fg=APP_HDR_FG,
            activebackground=APP_NAVY,
            activeforeground=APP_HDR_FG,
            selectcolor=APP_NAVY2,
            font=("Segoe UI", 8),
            borderwidth=0,
            highlightthickness=0,
        )
        keyboard_toggle.pack(side=tk.LEFT, padx=(0, 2), pady=8)

    def _tick_header(self):
        """Refresh the header status line with live temperature data (if connected)."""
        try:
            if not self.winfo_exists():
                return
            parts = []
            temp = data_store.get("Temperature")
            if temp is not None:
                parts.append(f"T: {temp:.1f} °C")
            sp = data_store.get("SetTemperature")
            if sp is not None:
                parts.append(f"SP: {sp:.1f} °C")
            power = data_store.get("Power")
            if power is not None:
                parts.append(f"Power: {power:.0f}%")
            if parts:
                self._hdr_status.config(text="  |  ".join(parts))
            self.after(600, self._tick_header)
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
    app_main = CombinedApp()
    try:
        app_main.mainloop()
    except KeyboardInterrupt:
        try:
            app_main.on_close()
        except Exception:
            pass
