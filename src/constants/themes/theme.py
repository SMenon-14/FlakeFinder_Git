# ── theme.py ──────────────────────────────────────────────────────────────────
import tkinter as tk
from tkinter import ttk

# ── Colour Palette ────────────────────────────────────────────────────────────

C_BG        = "#CFCFCF"
C_SURFACE   = "#d8d8d8"
C_SURFACE2  = "#f8f8f8"
C_ACCENT    = "#5b8dee"
C_ACCENT2   = "#3a6fd8"
C_TEXT      = "#1a1a2e"
C_MUTED     = "#8888aa"
C_BORDER    = "#3e3e58"
C_USED      = "#B03030"
C_AVAIL     = "#1A7A3F"
C_ROW_ALT   = "#FFFFFF"
C_SIDEBAR   = "#1a1a2e"
C_SIDEBAR_H = "#2a2a4e"
C_SIDEBAR_A = "#5b8dee"

# ── Fonts ─────────────────────────────────────────────────────────────────────

FONT_MONO   = ("Courier New", 9)
FONT_BODY   = ("Segoe UI", 10)
FONT_SMALL  = ("Segoe UI", 8)
FONT_TITLE  = ("Segoe UI Semibold", 11)
FONT_HEAD   = ("Segoe UI", 9)

# ── Shared Widget Helpers ─────────────────────────────────────────────────────

def setup_ttk_styles() -> None:
    """Configures global TTK overrides to drop native OS drawing styles for absolute hex color control."""
    style = ttk.Style()
    style.theme_use('clam')  # Bypasses native Aqua rendering engine blocks
    
    # Primary Navigation Button Theme Style
    style.configure(
        "Primary.TButton",
        background=C_ACCENT,
        foreground="white",
        font=("Segoe UI Semibold", 10),
        relief="flat",
        borderwidth=0,
        focuscolor="none"
    )
    style.map(
        "Primary.TButton",
        background=[("active", C_ACCENT2), ("pressed", C_ACCENT2)],
        foreground=[("active", "white"), ("pressed", "white")]
    )
    
    # Secondary Layout Dashboard Style
    style.configure(
        "Secondary.TButton",
        background=C_SURFACE,
        foreground=C_TEXT,
        font=FONT_BODY,
        relief="flat",
        borderwidth=0,
        focuscolor="none"
    )
    style.map(
        "Secondary.TButton",
        background=[("active", C_BORDER), ("pressed", C_BORDER)],
        foreground=[("active", "white"), ("pressed", "white")]
    )

def apply_button_style(btn: ttk.Button, primary: bool = True) -> None:
    """Applies cross-platform hex style rule patterns to target TTK components."""
    if primary:
        btn.configure(style="Primary.TButton")
    else:
        btn.configure(style="Secondary.TButton")


def make_separator(parent: tk.Widget, color: str = C_BORDER,
                   thickness: int = 1) -> tk.Frame:
    """Return a thin horizontal separator frame."""
    return tk.Frame(parent, bg=color, height=thickness)


def entry_with_placeholder(parent: tk.Widget, placeholder: str,
                            show: str | None = None,
                            width: int = 28) -> tk.Frame:
    """Bordered entry widget with placeholder-text behaviour."""
    frame = tk.Frame(
        parent, bg=C_SURFACE2,
        highlightbackground=C_BORDER, highlightthickness=1,
    )

    var = tk.StringVar()
    e = tk.Entry(
        frame, textvariable=var, font=FONT_BODY,
        bg=C_SURFACE2, fg=C_MUTED, relief="flat",
        insertbackground=C_TEXT, width=width,
        show="",
    )
    e.pack(padx=10, pady=8, fill="x")

    e._placeholder = placeholder
    e._show        = show
    e._active      = False

    def on_focus_in(_event):
        if not e._active:
            e._active = True
            e.config(fg=C_TEXT)
            if show:
                e.config(show=show)
            if var.get() == placeholder:
                var.set("")

    def on_focus_out(_event):
        if var.get() == "":
            e._active = False
            e.config(fg=C_MUTED, show="")
            var.set(placeholder)

    var.set(placeholder)
    e.bind("<FocusIn>",  on_focus_in)
    e.bind("<FocusOut>", on_focus_out)

    # Border highlight on hover
    def _highlight_on(_e):  frame.config(highlightbackground=C_ACCENT)
    def _highlight_off(_e): frame.config(highlightbackground=C_BORDER)
    for widget in (frame, e):
        widget.bind("<Enter>", _highlight_on)
        widget.bind("<Leave>", _highlight_off)

    frame.focus_widget = e
    frame.get_value    = lambda: var.get() if e._active else ""
    frame.var          = var

    return frame