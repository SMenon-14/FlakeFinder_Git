# ── home_view.py ──────────────────────────────────────────────────────────────
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox

import src.constants.themes.theme as theme
from src.menu.home_controller import HomeController, StatCard, ActivityRow

_STAT_COLORS = {
    "accent": theme.C_ACCENT,
    "avail":  theme.C_AVAIL,
    "used":   theme.C_USED,
    "muted":  theme.C_MUTED,
}

_STATUS_BADGE_COLORS = {
    "In Progress": theme.C_ACCENT,
    "Complete":    theme.C_AVAIL,
    "Pending":     theme.C_USED,
}


class HomeView(tk.Tk):
    def __init__(self, controller: HomeController | None = None) -> None:
        super().__init__()
        self._ctrl = controller or HomeController()
        self._ctrl.add_page_observer(self._on_page_changed)

        self.withdraw()
        self.title("FlakeFinder")
        self.configure(bg=theme.C_BG)

        # Initialize cross-platform hex style themes
        theme.setup_ttk_styles()

        w, h = 1100, 680
        self.geometry(
            f"{w}x{h}+"
            f"{(self.winfo_screenwidth()  - w) // 2}+"
            f"{(self.winfo_screenheight() - h) // 2}"
        )
        self.minsize(900, 560)

        self._nav_buttons: dict[str, tk.Button] = {}
        self._build_shell()

    def show_after_login(self) -> None:
        self._user_label.config(text=self._ctrl.username)
        initial = self._ctrl.username[0].upper() if self._ctrl.username else "U"
        self._avatar_canvas.itemconfig(self._avatar_text_id, text=initial)
        self.deiconify()
        self._ctrl.set_current_page("dashboard")

    def _build_shell(self) -> None:
        tk.Frame(self, bg=theme.C_ACCENT, height=3).pack(fill="x", side="top")

        body = tk.Frame(self, bg=theme.C_BG)
        body.pack(fill="both", expand=True)

        tk.Frame(body, bg=theme.C_BORDER, width=1).pack(side="left", fill="y")

        content_col = tk.Frame(body, bg=theme.C_BG)
        content_col.pack(side="left", fill="both", expand=True)
        self._build_topbar(content_col)
        self._build_dashboard(content_col)

    def _build_topbar(self, parent: tk.Frame) -> None:
        topbar = tk.Frame(parent, bg=theme.C_SURFACE,
                          highlightbackground=theme.C_BORDER, highlightthickness=1,
                          height=48)
        topbar.pack(fill="x")
        topbar.pack_propagate(False)

        self._page_title_lbl = tk.Label(
            topbar, text="Dashboard",
            font=("Segoe UI Semibold", 13), fg=theme.C_TEXT, bg=theme.C_SURFACE,
        )
        self._page_title_lbl.pack(side="left", padx=22)

        right = tk.Frame(topbar, bg=theme.C_SURFACE)
        right.pack(side="right", padx=16)

        # Swapped "Sign Out" to a flat tk.Label behaving like a hyperlink text block.
        # This completely fixes the light background macOS button contrast engine failure.
        lbl_signout = tk.Label(
            right, text="Sign Out", font=theme.FONT_SMALL,
            fg=theme.C_MUTED, bg=theme.C_SURFACE, cursor="hand2"
        )
        lbl_signout.pack(side="right", padx=(8, 0))
        lbl_signout.bind("<Enter>", lambda e: lbl_signout.config(fg=theme.C_USED))
        lbl_signout.bind("<Leave>", lambda e: lbl_signout.config(fg=theme.C_MUTED))
        lbl_signout.bind("<Button-1>", lambda e: self._on_sign_out())

        tk.Label(right, text="|", fg=theme.C_BORDER,
                 bg=theme.C_SURFACE, font=theme.FONT_BODY).pack(side="right")

        self._user_label = tk.Label(right, text="",
                                    font=("Segoe UI Semibold", 10),
                                    fg=theme.C_TEXT, bg=theme.C_SURFACE)
        self._user_label.pack(side="right", padx=(0, 8))

        self._avatar_canvas = tk.Canvas(right, width=28, height=28,
                                        bg=theme.C_SURFACE, highlightthickness=0)
        self._avatar_canvas.pack(side="right")
        self._avatar_canvas.create_oval(2, 2, 26, 26, fill=theme.C_ACCENT, outline="")
        self._avatar_text_id = self._avatar_canvas.create_text(
            14, 14, text="U", fill="white", font=("Segoe UI Semibold", 10),
        )

    def _build_dashboard(self, parent: tk.Frame) -> None:
        scroll_outer = tk.Frame(parent, bg=theme.C_BG)
        scroll_outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(scroll_outer, bg=theme.C_BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(scroll_outer, orient="vertical",
                                  command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg=theme.C_BG)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(win_id, width=e.width))
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        p = inner

        # ── Greeting ──────────────────────────────────────────────────────────
        hdr = tk.Frame(p, bg=theme.C_BG)
        hdr.pack(fill="x", padx=28, pady=(24, 6))
        self._greeting_lbl = tk.Label(
            hdr, text=f"Hello, {self._ctrl.username}",
            font=("Segoe UI Semibold", 16), fg=theme.C_TEXT, bg=theme.C_BG,
        )

        # ── Quick-launch tiles ────────────────────────────────────────────────
        ql_section = tk.Frame(p, bg=theme.C_BG)
        ql_section.pack(fill="x", padx=28, pady=(28, 6))
        tk.Label(ql_section, text="Quick Launch", font=theme.FONT_TITLE,
                 fg=theme.C_TEXT, bg=theme.C_BG).pack(anchor="w")
        theme.make_separator(ql_section, theme.C_BORDER).pack(fill="x", pady=(8, 0))

        tiles_row = tk.Frame(p, bg=theme.C_BG)
        tiles_row.pack(fill="x", padx=28, pady=(0, 0))

        for i, nav in enumerate(self._ctrl.NAV_ITEMS):
            if nav.key == "dashboard":
                continue
            col = i - 1   
            tile = tk.Frame(tiles_row, bg=theme.C_SURFACE2,
                            highlightbackground=theme.C_BORDER, highlightthickness=1,
                            cursor="hand2")
            tile.grid(row=0, column=col, padx=(0, 12), sticky="nsew", ipady=6)
            tiles_row.columnconfigure(col, weight=1)

            icon_lbl = nav.label.split("  ", 1)[0] if "  " in nav.label else "▶"
            title_txt = nav.label.split("  ", 1)[-1] if "  " in nav.label else nav.label

            tk.Frame(tile, bg=theme.C_ACCENT, height=3).pack(fill="x")
            inner_t = tk.Frame(tile, bg=theme.C_SURFACE2)
            inner_t.pack(padx=14, pady=12, fill="x")
            tk.Label(inner_t, text=icon_lbl, font=("Segoe UI", 18),
                     fg=theme.C_ACCENT, bg=theme.C_SURFACE2).pack(anchor="w")
            tk.Label(inner_t, text=title_txt, font=theme.FONT_TITLE,
                     fg=theme.C_TEXT, bg=theme.C_SURFACE2).pack(anchor="w", pady=(4, 2))
            tk.Label(inner_t, text=nav.description, font=theme.FONT_SMALL,
                     fg=theme.C_MUTED, bg=theme.C_SURFACE2, wraplength=160,
                     justify="left").pack(anchor="w")

            # FIXED: Swapped to an actual ttk.Button instance to allow flat styled theming properties
            launch_btn = ttk.Button(
                inner_t, text="Open →",
                command=lambda k=nav.key: self._on_nav_click(k),
            )
            theme.apply_button_style(launch_btn, primary=True)
            launch_btn.pack(anchor="w", pady=(8, 0))

            # Whole tile is also clickable
            for w in (tile, inner_t):
                w.bind("<Button-1>", lambda e, k=nav.key: self._on_nav_click(k))


    def _on_nav_click(self, key: str) -> None:
        """Launch the mapped script and close this window on success."""

        success, error = self._ctrl.launch(key)

        if success:
            #self.destroy()
            pass
        else:
            self._ctrl.set_current_page("dashboard")
            messagebox.showerror("Launch Error", error, parent=self)

    def _on_page_changed(self, key: str) -> None:
        self._page_title_lbl.config(text=self._ctrl.page_title(key))
        if hasattr(self, "_greeting_lbl"):
            self._greeting_lbl.config(
                text=f"Good morning, {self._ctrl.username} 👋"
            )

    def _on_sign_out(self) -> None:
        if messagebox.askyesno("Sign Out",
                               "Are you sure you want to sign out?",
                               parent=self):
            self.withdraw()
            self.event_generate("<<SignOut>>")