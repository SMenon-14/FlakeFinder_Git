import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk

from src.browser.constants import (
    C_BG, C_SURFACE2, C_TEXT, C_MUTED, C_USED, C_AVAIL,
    FONT_BODY, FONT_HEAD,
    DETAIL_IMG,
)
from src.browser.database import FlakeDatabase
from src.browser.widgets import StyledButton, ToggleButton, StatusBadge


class DetailPage(tk.Frame):

    def __init__(self, parent, db: FlakeDatabase, on_back):
        super().__init__(parent, bg=C_BG)
        self.db      = db
        self.on_back = on_back
        self._flake  = None
        self._showing_outlined = False
        self._photo  = None
        self._build_ui()

    def _build_ui(self):
        # Top bar
        bar = tk.Frame(self, bg=C_BG, padx=10, pady=8)
        bar.pack(fill="x")
        StyledButton(bar, "← Return to Search",
                     command=self.on_back, active=True).pack(side="left")

        # Body
        body = tk.Frame(self, bg=C_BG, padx=16, pady=8)
        body.pack(fill="both", expand=True)

        # Left — image
        left = tk.Frame(body, bg=C_BG)
        left.pack(side="left", anchor="n", padx=(0, 20))

        self._img_label = tk.Label(left, bg=C_SURFACE2,
                                   width=DETAIL_IMG[0], height=DETAIL_IMG[1])
        self._img_label.pack()

        btn_row = tk.Frame(left, bg=C_BG)
        btn_row.pack(pady=(6, 0))
        self._btn_normal   = StyledButton(btn_row, "Normal",
                                          command=self._show_normal, active=True)
        self._btn_outlined = StyledButton(btn_row, "Outlined",
                                          command=self._show_outlined)
        self._btn_normal.pack(side="left", padx=(0, 4))
        self._btn_outlined.pack(side="left")

        # Right — metadata
        right = tk.Frame(body, bg=C_BG)
        right.pack(side="left", anchor="n", fill="both", expand=True)

        self._info_frame = tk.Frame(right, bg=C_BG)
        self._info_frame.pack(fill="x")

        action_row = tk.Frame(right, bg=C_BG)
        action_row.pack(fill="x", pady=(12, 0))
        self._mark_used_btn = ToggleButton(action_row, "Mark Used",
                                           on_toggle=self._on_mark_used)
        self._mark_used_btn.pack(side="left")

        other_frame = tk.Frame(right, bg=C_BG)
        other_frame.pack(fill="x", pady=(12, 0))
        tk.Label(other_frame, text="Other Flakes in This Image",
                 bg=C_BG, fg=C_MUTED, font=FONT_HEAD).pack(anchor="w")
        self._other_var   = tk.StringVar()
        self._other_combo = ttk.Combobox(other_frame,
                                         textvariable=self._other_var,
                                         state="readonly", width=36,
                                         font=FONT_BODY)
        self._other_combo.pack(anchor="w", pady=(2, 0))
        self._other_combo.bind("<<ComboboxSelected>>", self._on_other_selected)

    # ── Load ──────────────────────────────────────────────────────────────────

    def load(self, flake: dict):
        self._flake = flake
        self._showing_outlined = False
        self._btn_normal.set_active(True)
        self._btn_outlined.set_active(False)
        self._render_image_normal()
        self._render_info()
        self._load_other_flakes()
        self._mark_used_btn.set(flake.get("used", False))

    def _render_image_normal(self):
        self._render_image_from_pil(None)   # signals: load from file

    def _render_image_from_pil(self, pil_img: Image.Image | None):
        """If pil_img is None, load from the flake's image path."""
        try:
            if pil_img is None:
                pil_img = Image.open(self._flake.get("_image_path", ""))
            pil_img.thumbnail(DETAIL_IMG, Image.LANCZOS)
            self._photo = ImageTk.PhotoImage(pil_img)
            self._img_label.config(image=self._photo,
                                   text="",
                                   width=self._photo.width(),
                                   height=self._photo.height())
        except Exception:
            self._img_label.config(image="", text="Image not found",
                                   fg=C_MUTED, font=FONT_BODY)

    def _render_info(self):
        for w in self._info_frame.winfo_children():
            w.destroy()

        f = self._flake
        max_s = f.get("max_sidelength_um")
        min_s = f.get("min_sidelength_um")
        size_str = (f"{max_s:.2f} µm × {min_s:.2f} µm"
                    if max_s is not None else "—")

        fields = [
            ("Flake ID",        f.get("flake_id",        "—")),
            ("Material",        f.get("material",        "—")),
            ("Substrate",       f.get("substrate",       "—")),
            ("Layers",          f"{f.get('layers', '—')} layers"),
            ("Thickness",       f"{f.get('thickness_nm', '—')} nm"),
            ("Size (max×min)",  size_str),
            ("(row, col)",      f.get("(row, col)",      "—")),
            ("Session",         f.get("_session",        "—")),
            ("Chip",            f.get("_chip_num",       "—")),
            ("Date Exfoliated", f.get("date_exfoliated", "—")),
            ("Date Scanned",    f.get("date_scanned",    "—")),
        ]
        for label, value in fields:
            row = tk.Frame(self._info_frame, bg=C_BG)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label + ":", bg=C_BG, fg=C_MUTED,
                     font=FONT_HEAD, width=18, anchor="w").pack(side="left")
            tk.Label(row, text=value, bg=C_BG, fg=C_TEXT,
                     font=FONT_BODY, anchor="w").pack(side="left")

        badge_row = tk.Frame(self._info_frame, bg=C_BG)
        badge_row.pack(fill="x", pady=6)
        tk.Label(badge_row, text="Usage Status:", bg=C_BG, fg=C_MUTED,
                 font=FONT_HEAD, width=18, anchor="w").pack(side="left")
        self._status_badge = StatusBadge(badge_row, f.get("used", False))
        self._status_badge.pack(side="left")

    # ── Image View Toggle ─────────────────────────────────────────────────────

    def _show_normal(self):
        self._showing_outlined = False
        self._btn_normal.set_active(True)
        self._btn_outlined.set_active(False)
        self._render_image_from_pil(None)

    def _show_outlined(self):
        if not self._flake:
            return
        contours = self.db.contour_paths(self._flake)
        if not contours:
            messagebox.showinfo("Not Found",
                                "No .npy contour files found for this flake.")
            return
        pil_img = self.db.build_outlined_image(self._flake)
        if pil_img is None:
            messagebox.showerror("Error", "Could not build outlined image.")
            return
        self._showing_outlined = True
        self._btn_outlined.set_active(True)
        self._btn_normal.set_active(False)
        self._render_image_from_pil(pil_img)

    # ── Mark Used ─────────────────────────────────────────────────────────────

    def _on_mark_used(self, toggled: bool):
        if self._flake:
            self.db.set_used(self._flake, toggled)
            self._status_badge.config(
                text = "Used" if toggled else "Available",
                bg   = C_USED if toggled else C_AVAIL,
            )

    # ── Other Flakes ──────────────────────────────────────────────────────────

    def _load_other_flakes(self):
        if not self._flake:
            return
        json_path  = self._flake.get("_json_path", "")
        current_id = self._flake.get("flake_id")
        siblings   = [f for f in self.db.flakes
                      if f.get("_json_path") == json_path
                      and f.get("flake_id") != current_id]
        labels = [
            f"Flake {f.get('flake_id','?')} — "
            f"{f.get('material','?')} {f.get('layers','?')}L "
            f"({f.get('max_sidelength_um', 0):.1f} µm)"
            for f in siblings
        ]
        self._other_combo["values"] = labels
        self._other_combo.set("")
        self._siblings = siblings

    def _on_other_selected(self, _=None):
        idx = self._other_combo.current()
        if 0 <= idx < len(self._siblings):
            self.load(self._siblings[idx])
