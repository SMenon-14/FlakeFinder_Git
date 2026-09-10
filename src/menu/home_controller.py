# ── home_controller.py ────────────────────────────────────────────────────────
# Application state and launch logic for the home / main window.
# No tkinter imports — purely Python data and state management.

from __future__ import annotations

import subprocess
import sys
import os
import importlib.util
import src.menu.authentication.encrypt as encrypt
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from src.paths import APP_ROOT


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class NavItem:
    label:  str          # display text (may include icon prefix)
    key:    str          # internal routing key
    script: str          # path to the child script, relative to this file
    description: str     # short subtitle shown on the launch card


@dataclass
class StatCard:
    title:     str
    value:     str
    color_key: str       # one of: "accent", "avail", "used", "muted"
    subtitle:  str


@dataclass
class ActivityRow:
    ref:    str
    item:   str
    status: str
    date:   str
    owner:  str


# ── Controller ────────────────────────────────────────────────────────────────

class HomeController:
    """
    Owns all non-UI state for the home window:
      - current user session
      - active navigation selection (for sidebar highlight only)
      - subprocess launching of child GUIs
      - data providers for the dashboard

    Navigation no longer switches in-process frames; instead `launch(key)`
    starts the mapped script in a subprocess and destroys the launcher window.
    The dashboard is the only page rendered inside this process.
    """

    # ── Nav / script registry ─────────────────────────────────────────────────
    # Set `script` to the real relative (or absolute) path for each tool.
    # The dashboard key is special — it never launches a subprocess.
    BROWSER_FPATH = 'src.browser.flake_browser_main'
    MICROSCOPE_FPATH = 'src.microscope.ONWAY_CombinedControl_app'
    HISTORY_FPATH = 'src.history.scan_history'
    CALIBRATION_FPATH = 'src.calibration.calibration_ui'
    ML_FPATH = 'src.autoscan.ffm.run_gui'
    TEMP_CONTROL_FPATH = 'src.temp_control.TS2_TempControl'

    NAV_ITEMS: list[NavItem] = [
        NavItem("⊞  Dashboard",  "dashboard",  "",                      "Overview and recent activity"),
        NavItem("≡  Browse for Flakes",    "browser",    BROWSER_FPATH,    "Browse for exfoliated flakes"),
        NavItem("◑  Access Microscope Controls",  "microscope",  MICROSCOPE_FPATH,  "Access microscope controls and start scans"),
        NavItem("◫  Review Scan History",   "history",   HISTORY_FPATH,   "View and manage past scans"),
        NavItem("⚙  Calibrate Constants",   "calibrate",   CALIBRATION_FPATH,   "Configure application preferences"),
        NavItem("✵  Run ML Detection",   "ml",   ML_FPATH,   "Manually run detection on chip images"),
        NavItem("◔  Temperature Control",   "temp_control",   TEMP_CONTROL_FPATH,   "Monitor and control stage temperature"),
    ]

    def __init__(self, base_dir: str | Path | None = None) -> None:
        # base_dir is the directory that contains main.py / the script files.
        # Defaults to the directory of this file.
        self._base_dir       = Path(base_dir) if base_dir else Path(__file__).parent
        self._username: str  = "User"
        self._current_page   = "dashboard"
        self._page_observers: list[Callable[[str], None]] = []

    # ── Session ───────────────────────────────────────────────────────────────

    def set_user(self, username: str) -> None:
        self._username = username

    @property
    def username(self) -> str:
        return self._username

    # ── Navigation state (sidebar highlight) ─────────────────────────────────

    @property
    def current_page(self) -> str:
        return self._current_page

    def set_current_page(self, key: str) -> None:
        """Update the highlighted nav item and notify observers."""
        self._current_page = key
        for cb in self._page_observers:
            cb(key)

    def add_page_observer(self, cb: Callable[[str], None]) -> None:
        self._page_observers.append(cb)

    def page_title(self, key: str) -> str:
        for item in self.NAV_ITEMS:
            if item.key == key:
                lbl = item.label
                return lbl.split("  ", 1)[-1] if "  " in lbl else lbl
        return key.title()

    def get_nav_item(self, key: str) -> NavItem | None:
        return next((n for n in self.NAV_ITEMS if n.key == key), None)

    # ── Subprocess launcher ───────────────────────────────────────────────────
    def module_exists(self, module_name: str) -> bool:
        """Check if a module can be imported without actually importing it."""
        try:
            return importlib.util.find_spec(module_name) is not None
        except ModuleNotFoundError:
            return False


    def launch(self, key: str) -> tuple[bool, str]:
        """
        Resolve and launch the script mapped to `key` in a new subprocess.

        Returns (success: bool, error_message: str).
        The caller (view) is responsible for closing the launcher window
        after a successful launch.
        """
        item = self.get_nav_item(key)
        if item is None:
            return False, f"Unknown page key: {key!r}"

        if not item.script:
            return False, "No script configured for this item."

        script_path = item.script
        if not self.module_exists(script_path):
            return False, (
                f"Script not found:\n{script_path}\n\n"
                "Add the file or update the path in home_controller.py."
            )

        try:
            subprocess.Popen(
                [sys.executable, '-m',str(script_path)],
                cwd=str(APP_ROOT),
            )
            return True, ""
        except OSError as exc:
            return False, f"Failed to launch script:\n{exc}"
