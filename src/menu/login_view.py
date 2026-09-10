# ── login_view.py ─────────────────────────────────────────────────────────────
# Login window UI. All authentication decisions are delegated to
# LoginController; this file contains zero business logic.

from __future__ import annotations
from typing import Callable

import tkinter as tk
from tkinter import ttk  # Required for modern themed button components

import src.constants.themes.theme as theme
from src.constants.themes.theme import (
    C_BG, C_SURFACE2, C_BORDER, C_ACCENT, C_TEXT, C_MUTED, C_USED,
    FONT_BODY, FONT_SMALL,
    apply_button_style, make_separator, entry_with_placeholder,
)
from src.menu.login_controller import LoginController


class LoginView(tk.Toplevel):
    """
    Modal login dialog.

    Parameters
    ----------
    master      : parent Tk window (kept hidden behind this dialog)
    on_success  : callback(username: str) called when authentication succeeds
    controller  : optional LoginController injection; a default is created if
                  omitted (makes unit-testing easy by injecting a mock)
    """

    def __init__(
        self,
        master: tk.Tk,
        on_success: Callable[[str], None],
        controller: LoginController | None = None,
    ):
        super().__init__(master)
        self._on_success  = on_success
        self._controller  = controller or LoginController()

        self.title("Sign In")
        self.resizable(False, False)
        self.configure(bg=C_BG)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        # Ensure themed button styles are initialized if this runs standalone
        theme.setup_ttk_styles()

        w, h = 420, 540
        self.geometry(
            f"{w}x{h}+"
            f"{(self.winfo_screenwidth()  - w) // 2}+"
            f"{(self.winfo_screenheight() - h) // 2}"
        )

        self._build()
        self.grab_set()

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build(self) -> None:
        # Top accent bar
        tk.Frame(self, bg=C_ACCENT, height=4).pack(fill="x")

        outer = tk.Frame(self, bg=C_BG)
        outer.pack(expand=True, fill="both", padx=40, pady=30)

        card = tk.Frame(outer, bg=C_SURFACE2,
                        highlightbackground=C_BORDER, highlightthickness=1)
        card.pack(fill="both", expand=True)

        inner = tk.Frame(card, bg=C_SURFACE2)
        inner.pack(padx=36, pady=36, fill="both", expand=True)

        # Logo row
        logo_row = tk.Frame(inner, bg=C_SURFACE2)
        logo_row.pack(anchor="w", pady=(0, 4))
        tk.Label(logo_row, text="●", fg=C_ACCENT,
                 font=("Segoe UI", 18), bg=C_SURFACE2).pack(side="left")
        tk.Label(logo_row, text=" FlakeFinder", fg=C_TEXT, bg=C_SURFACE2,
                 font=("Segoe UI Semibold", 18)).pack(side="left")

        make_separator(inner, C_BORDER).pack(fill="x", pady=(14, 20))

        # Heading
        tk.Label(inner, text="Welcome back",
                 font=("Segoe UI Semibold", 15), fg=C_TEXT,
                 bg=C_SURFACE2).pack(anchor="w")
        tk.Label(inner, text="Sign in to continue",
                 font=FONT_BODY, fg=C_MUTED,
                 bg=C_SURFACE2).pack(anchor="w", pady=(2, 20))

        # Username field
        tk.Label(inner, text="USERNAME", font=("Segoe UI Semibold", 8),
                 fg=C_MUTED, bg=C_SURFACE2).pack(anchor="w")
        self._user_entry = entry_with_placeholder(inner, "Enter your username")
        self._user_entry.pack(fill="x", pady=(4, 14))

        # Password field
        tk.Label(inner, text="PASSWORD", font=("Segoe UI Semibold", 8),
                 fg=C_MUTED, bg=C_SURFACE2).pack(anchor="w")
        self._pass_entry = entry_with_placeholder(
            inner, "Enter your password", show="●"
        )
        self._pass_entry.pack(fill="x", pady=(4, 6))

        # Forgot password link
        #tk.Label(inner, text="Forgot password?", font=FONT_SMALL,
        #         fg=C_ACCENT, bg=C_SURFACE2, cursor="hand2"
        #         ).pack(anchor="e", pady=(0, 20))

        # Inline error message
                                    
        self._err_var = tk.StringVar()
        tk.Label(inner, textvariable=self._err_var, font=FONT_SMALL,
                 fg=C_USED, bg=C_SURFACE2).pack(anchor="w", pady=(0, 4))

        # FIXED: Converted to a themed ttk.Button component to prevent Mac contrast ghosting 
        btn = ttk.Button(inner, text="Sign In", command=self._on_submit)
        apply_button_style(btn, primary=True)
        btn.pack(fill="x")

        self.bind("<Return>", lambda _e: self._on_submit())

        # Footer
        make_separator(inner, C_BORDER).pack(fill="x", pady=(24, 16))
        lbl_register = tk.Label(inner, text="Don't have an account?  Register with Entered Credentials →",
                 font=FONT_SMALL, fg=C_MUTED, bg=C_SURFACE2,
                 cursor="hand2")
        lbl_register.pack()
        lbl_register.bind("<Enter>", lambda e: lbl_register.config(fg=theme.C_USED))
        lbl_register.bind("<Leave>", lambda e: lbl_register.config(fg=theme.C_MUTED))
        lbl_register.bind("<Button-1>", lambda e: self._on_register())

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_submit(self) -> None:
        """Read form values, delegate auth, then react to the result."""
        username = self._user_entry.get_value()
        password = self._pass_entry.get_value()

        result = self._controller.authenticate(username, password)

        if not result.success:
            self._err_var.set(result.error)
            return

        self._err_var.set("")
        self.destroy()
        self._on_success(result.username)

    def _on_register(self):
        username = self._user_entry.get_value()
        password = self._pass_entry.get_value()

        self._controller.register_user(username, password)

        self.destroy()
        self._on_success(username)