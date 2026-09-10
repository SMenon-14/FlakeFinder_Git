"""Shared config, fonts, colors, and ttk style for the ONWAY combined control UI."""
import os
import configparser
import tkinter as tk
from tkinter import ttk

CFG_PATH = os.path.join(os.path.dirname(__file__), "TempConfig.ini")
cfg = configparser.ConfigParser()
if not cfg.read(CFG_PATH, encoding="utf-8"):
    raise FileNotFoundError(f"[Config] Cannot read {CFG_PATH}")

# ---- fonts / UI ----
BASE_FONT_SIZE  = cfg.getint("UI", "base_font_size",  fallback=20)
BIG_FONT_SIZE   = cfg.getint("UI", "big_font_size",   fallback=82)
UNIT_FONT_SIZE  = cfg.getint("UI", "unit_font_size",  fallback=32)
ENTRY_FONT_SIZE = cfg.getint("UI", "entry_font_size", fallback=18)

# Global UI scale (smaller fonts/buttons)
UI_SCALE = float(cfg.get("UI", "ui_scale", fallback="0.60"))

BASE_FONT       = ("Arial", max(8, int(BASE_FONT_SIZE * UI_SCALE)))
BASE_FONT_BOLD  = ("Arial", max(9, int((BASE_FONT_SIZE + 2) * UI_SCALE)), "bold")
BIG_FONT        = ("Arial", max(16, int(BIG_FONT_SIZE * UI_SCALE)), "bold")
UNIT_FONT       = ("Arial", max(10, int(UNIT_FONT_SIZE * UI_SCALE)))
ENTRY_FONT      = ("Arial", max(9, int(ENTRY_FONT_SIZE * UI_SCALE)))
ICON_PATH = cfg.get("UI", "icon_path", fallback="")

# ── App-wide colour palette ───────────────────────────────────────────────────
APP_NAVY    = "#1A4F8A"   # header / accent
APP_NAVY2   = "#2471B5"   # hover
APP_SUCCESS = "#1A7A3F"   # connect / start / go
APP_DANGER  = "#B03030"   # disconnect / stop
APP_BG      = "#F0F2F6"   # window background
APP_PANEL   = "#FFFFFF"   # card / panel background
APP_BORDER  = "#C8D0DA"   # separator / border
APP_TEXT    = "#1C2833"   # primary text
APP_MUTED   = "#5D6D7E"   # secondary text
APP_HDR_FG  = "#FFFFFF"   # text on dark header

def configure_app_style():
    """Apply a cohesive Navy-and-White theme to all ttk widgets.
    Must be called after a Tk root window is created."""
    s = ttk.Style()
    try:
        s.theme_use("clam")
    except tk.TclError:
        pass

    s.configure(".",
                 background=APP_BG, foreground=APP_TEXT,
                 font=("Segoe UI", 9))
    s.configure("TFrame",       background=APP_BG)
    s.configure("TLabel",       background=APP_BG, foreground=APP_TEXT)
    s.configure("TLabelframe",
                background=APP_BG, bordercolor=APP_BORDER,
                relief="solid", borderwidth=1)
    s.configure("TLabelframe.Label",
                background=APP_BG, foreground=APP_NAVY,
                font=("Segoe UI", 9, "bold"))

    # Default button — navy
    s.configure("TButton",
                background=APP_NAVY, foreground=APP_HDR_FG,
                font=("Segoe UI", 9), padding=(8, 3),
                relief="flat", borderwidth=0, focuscolor=APP_NAVY2)
    s.map("TButton",
          background=[("active", APP_NAVY2), ("pressed", APP_NAVY),
                      ("disabled", APP_BORDER)],
          foreground=[("disabled", APP_MUTED)])

    # Green — connect / start / run
    s.configure("Success.TButton",
                background=APP_SUCCESS, foreground=APP_HDR_FG,
                font=("Segoe UI", 9), padding=(8, 3),
                relief="flat", borderwidth=0)
    s.map("Success.TButton",
          background=[("active", "#228B4E"), ("pressed", APP_SUCCESS)])

    # Red — disconnect / stop
    s.configure("Danger.TButton",
                background=APP_DANGER, foreground=APP_HDR_FG,
                font=("Segoe UI", 9), padding=(8, 3),
                relief="flat", borderwidth=0)
    s.map("Danger.TButton",
          background=[("active", "#C0392B"), ("pressed", APP_DANGER)])

    # Checkbutton / Scale / Entry
    s.configure("TCheckbutton", background=APP_BG, foreground=APP_TEXT)
    s.map("TCheckbutton", background=[("active", APP_BG)])
    s.configure("TEntry",       fieldbackground=APP_PANEL, padding=3)
    s.configure("TCombobox",    fieldbackground=APP_PANEL)
    s.configure("TScale",       background=APP_BG, troughcolor=APP_BORDER)
    s.configure("TScrollbar",   background=APP_BORDER, troughcolor=APP_BG, arrowsize=12)
    s.configure("Horizontal.TProgressbar",
                troughcolor=APP_BORDER, background=APP_SUCCESS, borderwidth=0)
    s.configure("TSeparator",   background=APP_BORDER)

ACCENT_COLOR = APP_NAVY
WHITE_BG   = "#FFFFFF"
POP_FONT   = ("Arial", max(8, int((BASE_FONT_SIZE - 7) * UI_SCALE)))
BTN_FONT   = ("Arial", max(9, int(14 * UI_SCALE)))
LABEL_FONT = ("Segoe UI", max(8, int(9 * UI_SCALE)))
