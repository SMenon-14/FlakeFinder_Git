from __future__ import annotations

import os
import subprocess
import sys
import re
import shutil
import json
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk, ImageDraw
import numpy as np
from src.history.flake_annotator import open_annotator
from src.paths import IMAGES_DIR
import src.constants.themes.theme as theme



# ── Grid / Pagination Setup ──────────────────────────────────────────────────
GRID_COLS  = 5
GRID_ROWS  = 5
PAGE_SIZE  = GRID_COLS * GRID_ROWS
THUMB      = (160, 120)

# ── Contour Overlay Setup ─────────────────────────────────────────────────────
CONTOUR_COLOR = (0, 255, 0)
CONTOUR_WIDTH = 2


# ── Helper Functions ──────────────────────────────────────────────────────────

def format_date_string(date_str: str) -> str:
    """Converts a MMDDYY string into MM-DD-YYYY display format."""
    if len(date_str) == 6 and date_str.isdigit():
        return f"{date_str[0:2]}-{date_str[2:4]}-20{date_str[4:6]}"
    return date_str


def format_session_name(folder_name: str) -> str:
    """
    Converts 'ScanningSession_MMDDYY' to 'Session MM-DD-YYYY'.
    Falls back to the raw folder name if it doesn't match the expected pattern.
    """
    prefix = "ScanningSession_"
    if folder_name.startswith(prefix):
        date_part = folder_name[len(prefix):]
        return f"Session {format_date_string(date_part)}"
    return folder_name


def _point_in_polygon(x: float, y: float, poly) -> bool:
    """Ray-casting point-in-polygon test. `poly` is an iterable of (x, y) pairs."""
    inside = False
    n = len(poly)
    if n < 3:
        return False
    xj, yj = poly[-1]
    for xi, yi in poly:
        if (yi > y) != (yj > y):
            x_intersect = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_intersect:
                inside = not inside
        xj, yj = xi, yi
    return inside


# ── Main Application ──────────────────────────────────────────────────────────

class ImageBrowserApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()

        self.withdraw()
        self.title("Scan History — FlakeFinder")
        self.configure(bg=theme.C_BG)

        theme.setup_ttk_styles()

        w, h = 1100, 720
        self.geometry(
            f"{w}x{h}+"
            f"{(self.winfo_screenwidth()  - w) // 2}+"
            f"{(self.winfo_screenheight() - h) // 2}"
        )
        self.minsize(900, 560)

        # ── State ─────────────────────────────────────────────────────────────
        self.current_session    = None
        self.current_date       = None
        self.current_chip       = None   # stored as the exact folder string on disk
        self.filter_mode        = "Both"
        self.image_list: list   = []
        self.current_view_index = None

        # ── Grid pagination state ────────────────────────────────────────────
        self.current_page       = 0
        self._grid_generation   = 0
        self._grid_cell_labels: dict = {}

        # ── Viewer / contour overlay state ───────────────────────────────────
        self._show_contours       = False
        self._current_flake_data: list = []
        self._img_scale            = 1.0
        self._img_disp_size        = (0, 0)
        self._viewer_generation    = 0

        # ── Shell ─────────────────────────────────────────────────────────────
        self._build_shell()
        self.deiconify()

        # Initial view loaded into the content area
        self.show_session_selection()

    # ── Shell / Chrome ────────────────────────────────────────────────────────

    def _build_shell(self) -> None:
        """Builds the persistent outer chrome: accent bar, top-bar, content area."""
        tk.Frame(self, bg=theme.C_ACCENT, height=3).pack(fill="x", side="top")

        body = tk.Frame(self, bg=theme.C_BG)
        body.pack(fill="both", expand=True)

        tk.Frame(body, bg=theme.C_BORDER, width=1).pack(side="left", fill="y")

        content_col = tk.Frame(body, bg=theme.C_BG)
        content_col.pack(side="left", fill="both", expand=True)

        self._build_topbar(content_col)

        self._content_area = tk.Frame(content_col, bg=theme.C_BG)
        self._content_area.pack(fill="both", expand=True)

    def _build_topbar(self, parent: tk.Frame) -> None:
        topbar = tk.Frame(
            parent, bg=theme.C_SURFACE,
            highlightbackground=theme.C_BORDER, highlightthickness=1,
            height=48,
        )
        topbar.pack(fill="x")
        topbar.pack_propagate(False)

        self._page_title_lbl = tk.Label(
            topbar, text="Scan History",
            font=("Segoe UI Semibold", 13), fg=theme.C_TEXT, bg=theme.C_SURFACE,
        )
        self._page_title_lbl.pack(side="left", padx=22)

        right = tk.Frame(topbar, bg=theme.C_SURFACE)
        right.pack(side="right", padx=16)

        def _make_topbar_btn(text, fg_normal, command):
            lbl = tk.Label(
                right, text=text, font=theme.FONT_SMALL,
                fg=fg_normal, bg=theme.C_SURFACE, cursor="hand2",
            )
            lbl.bind("<Enter>",    lambda e: lbl.config(fg=theme.C_TEXT))
            lbl.bind("<Leave>",    lambda e: lbl.config(fg=fg_normal))
            lbl.bind("<Button-1>", lambda e: command())
            return lbl

        lbl_close = _make_topbar_btn("✕  Close", theme.C_USED, self.destroy)
        lbl_close.pack(side="right", padx=(8, 0))

        #lbl_home = _make_topbar_btn("⌂  Home", theme.C_MUTED, self._go_home)
        #lbl_home.pack(side="right", padx=(8, 0))

    def _go_home(self) -> None:
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "menu", "main.py")
        subprocess.Popen([sys.executable, os.path.normpath(script), "--no-login"])
        self.destroy()

    def _set_page_title(self, text: str) -> None:
        self._page_title_lbl.config(text=text)

    # ── Content Area Utilities ────────────────────────────────────────────────

    def _clear_content(self) -> None:
        """Destroys all child widgets in the content area and resets key bindings."""
        self.unbind("<Left>")
        self.unbind("<Right>")
        for w in self._content_area.winfo_children():
            w.destroy()

    def _make_scroll_canvas(self) -> tuple[tk.Canvas, tk.Frame]:
        """
        Creates a vertically-scrollable inner frame inside the content area.
        Returns (canvas, inner_frame).
        """
        outer = tk.Frame(self._content_area, bg=theme.C_BG)
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer, bg=theme.C_BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg=theme.C_BG)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width))
        inner.bind("<Configure>",  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        return canvas, inner

    def _build_section_header(
        self,
        parent:      tk.Frame,
        title:       str,
        back_action  = None,
    ) -> None:
        row = tk.Frame(parent, bg=theme.C_BG)
        row.pack(fill="x", padx=28, pady=(22, 4))

        if back_action:
            btn_back = ttk.Button(row, text="← Back", command=back_action)
            theme.apply_button_style(btn_back, primary=False)
            btn_back.pack(side="left", padx=(0, 14))

        tk.Label(
            row, text=title,
            font=theme.FONT_TITLE, fg=theme.C_TEXT, bg=theme.C_BG,
        ).pack(side="left", anchor="center")

        theme.make_separator(parent, theme.C_BORDER).pack(fill="x", padx=28, pady=(0, 4))

    # ── Tile Factory ─────────────────────────────────────────────────────────

    def _make_tile(
        self,
        parent:   tk.Frame,
        icon:     str,
        title:    str,
        grid_row: int,
        grid_col: int,
        command,
    ) -> None:
        """Quick Launch-style card tile, identical in structure to home_view."""
        tile = tk.Frame(
            parent, bg=theme.C_SURFACE2,
            highlightbackground=theme.C_BORDER, highlightthickness=1,
            cursor="hand2",
        )
        tile.grid(row=grid_row, column=grid_col, padx=(0, 12), pady=(0, 12),
                  sticky="nsew", ipady=4)

        tk.Frame(tile, bg=theme.C_ACCENT, height=3).pack(fill="x")

        inner_t = tk.Frame(tile, bg=theme.C_SURFACE2)
        inner_t.pack(padx=14, pady=10, fill="x")

        tk.Label(inner_t, text=icon, font=("Segoe UI", 16),
                 fg=theme.C_ACCENT, bg=theme.C_SURFACE2).pack(anchor="w")
        tk.Label(inner_t, text=title, font=theme.FONT_TITLE,
                 fg=theme.C_TEXT, bg=theme.C_SURFACE2).pack(anchor="w", pady=(3, 1))

        open_btn = ttk.Button(inner_t, text="Open →", command=command)
        theme.apply_button_style(open_btn, primary=True)
        open_btn.pack(anchor="w", pady=(7, 0))

        for w in (tile, inner_t):
            w.bind("<Button-1>", lambda e, cmd=command: cmd())

    # ── Step 1: Session Selection ─────────────────────────────────────────────

    def show_session_selection(self) -> None:
        self._clear_content()
        self._set_page_title("Scan History")
        _, inner = self._make_scroll_canvas()

        self._build_section_header(inner, "Select Scanning Session")

        grid_host = tk.Frame(inner, bg=theme.C_BG)
        grid_host.pack(fill="x", padx=28, pady=(8, 20))

        if not os.path.exists(IMAGES_DIR):
            os.makedirs(IMAGES_DIR, exist_ok=True)

        sessions = sorted(
            f for f in os.listdir(IMAGES_DIR)
            if f.startswith("ScanningSession_")
            and os.path.isdir(os.path.join(IMAGES_DIR, f))
        )

        if not sessions:
            tk.Label(
                grid_host,
                text=f"No sessions found in:\n{IMAGES_DIR}",
                font=theme.FONT_BODY, fg=theme.C_MUTED, bg=theme.C_BG,
            ).pack(pady=40)
            return

        COLS = 3
        for i, session in enumerate(sessions):
            grid_host.columnconfigure(i % COLS, weight=1)
            self._make_tile(
                grid_host, "≡", format_session_name(session),
                i // COLS, i % COLS,
                lambda s=session: self.select_session(s),
            )

    def select_session(self, session_name: str) -> None:
        self.current_session = session_name
        self.show_date_selection()

    # ── Step 2: Date Selection ────────────────────────────────────────────────

    def show_date_selection(self) -> None:
        self._clear_content()
        self._set_page_title(f"{format_session_name(self.current_session)}  ›  Date")
        _, inner = self._make_scroll_canvas()

        self._build_section_header(
            inner,
            f"{format_session_name(self.current_session)}  ›  Select Date",
            back_action=self.show_session_selection,
        )

        grid_host = tk.Frame(inner, bg=theme.C_BG)
        grid_host.pack(fill="x", padx=28, pady=(8, 20))

        session_path = os.path.join(IMAGES_DIR, self.current_session)
        dates: set[str] = set()

        if os.path.exists(session_path):
            for folder in os.listdir(session_path):
                if os.path.isdir(os.path.join(session_path, folder)):
                    clean = folder.replace("empty_", "").replace("_empty", "")
                    if len(clean) == 6 and clean.isdigit():
                        dates.add(clean)

        if not dates:
            tk.Label(
                grid_host,
                text="No valid date directories found.",
                font=theme.FONT_BODY, fg=theme.C_MUTED, bg=theme.C_BG,
            ).pack(pady=40)
            return

        COLS = 4
        for i, date_str in enumerate(sorted(dates)):
            grid_host.columnconfigure(i % COLS, weight=1)
            self._make_tile(
                grid_host, "◷", f"Date: {format_date_string(date_str)}",
                i // COLS, i % COLS,
                lambda d=date_str: self.select_date(d),
            )

    def select_date(self, date_str: str) -> None:
        self.current_date = date_str
        self.show_chip_selection()

    # ── Step 3: Chip Selection ────────────────────────────────────────────────

    def show_chip_selection(self) -> None:
        self._clear_content()
        display_date = format_date_string(self.current_date)
        self._set_page_title(f"{format_session_name(self.current_session)}  ›  {display_date}  ›  Chip")
        _, inner = self._make_scroll_canvas()

        self._build_section_header(
            inner,
            f"{format_session_name(self.current_session)}  ›  {display_date}  ›  Select Chip",
            back_action=self.show_date_selection,
        )

        grid_host = tk.Frame(inner, bg=theme.C_BG)
        grid_host.pack(fill="x", padx=28, pady=(8, 20))

        session_path = os.path.join(IMAGES_DIR, self.current_session)
        # Store exact folder names (preserves zero-padding like 0001, 001, 01, 1)
        chips: set[str] = set()

        for variant in (
            self.current_date,
            f"empty_{self.current_date}",
            f"{self.current_date}_empty",
        ):
            p = os.path.join(session_path, variant)
            if os.path.exists(p):
                for folder in os.listdir(p):
                    if os.path.isdir(os.path.join(p, folder)) and folder.isdigit():
                        chips.add(folder)   # keep as-is: "0001", "001", "1", etc.

        if not chips:
            tk.Label(
                grid_host,
                text="No chip directories found.",
                font=theme.FONT_BODY, fg=theme.C_MUTED, bg=theme.C_BG,
            ).pack(pady=40)
            return

        # Sort numerically (so 0001 < 0006 < 0009) regardless of padding
        sorted_chips = sorted(chips, key=lambda x: int(x))

        COLS = 5
        for i, chip_str in enumerate(sorted_chips):
            grid_host.columnconfigure(i % COLS, weight=1)
            self._make_tile(
                grid_host, "◫", f"Chip {chip_str}",
                i // COLS, i % COLS,
                lambda c=chip_str: self.select_chip(c),
            )

    def select_chip(self, chip_str: str) -> None:
        self.current_chip = chip_str   # exact folder name, e.g. "0001"
        self.current_page = 0
        self.show_image_grid()

    # ── Step 4: Image Grid ────────────────────────────────────────────────────

    def load_images_data(self) -> None:
        """Scans folder combinations to populate self.image_list."""
        self.image_list = []
        session_path = os.path.join(IMAGES_DIR, self.current_session)

        normal_path  = os.path.join(session_path, self.current_date,                self.current_chip)
        empty_prefix = os.path.join(session_path, f"empty_{self.current_date}",     self.current_chip)
        empty_suffix = os.path.join(session_path, f"{self.current_date}_empty",     self.current_chip)

        valid_ext = ('.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.tif')

        if os.path.exists(normal_path) and self.filter_mode in ("Both", "Non-Empty"):
            for f in sorted(os.listdir(normal_path)):
                if f.lower().endswith(valid_ext):
                    self.image_list.append({
                        "filename":  f,
                        "full_path": os.path.join(normal_path, f),
                        "is_empty":  False,
                    })

        for empty_path in (empty_prefix, empty_suffix):
            if os.path.exists(empty_path) and self.filter_mode in ("Both", "Empty"):
                for f in sorted(os.listdir(empty_path)):
                    if f.lower().endswith(valid_ext):
                        self.image_list.append({
                            "filename":  f,
                            "full_path": os.path.join(empty_path, f),
                            "is_empty":  True,
                        })

    def show_image_grid(self) -> None:
        """Step 4: Renders a paginated grid view; thumbnails for the current
        page are decoded on a background thread so opening a large folder
        doesn't block the UI."""
        self._clear_content()
        self.load_images_data()

        display_date = format_date_string(self.current_date)
        breadcrumb   = (
            f"{format_session_name(self.current_session)}"
            f"  ›  {display_date}"
            f"  ›  Chip {self.current_chip}"
        )
        self._set_page_title(breadcrumb)

        # ── Header row with filter pills ──────────────────────────────────────
        hdr_outer = tk.Frame(self._content_area, bg=theme.C_BG)
        hdr_outer.pack(fill="x", padx=28, pady=(18, 0))

        left_row = tk.Frame(hdr_outer, bg=theme.C_BG)
        left_row.pack(side="left")

        btn_back = ttk.Button(left_row, text="← Back", command=self.show_chip_selection)
        theme.apply_button_style(btn_back, primary=False)
        btn_back.pack(side="left", padx=(0, 14))

        tk.Label(
            left_row, text=breadcrumb,
            font=theme.FONT_TITLE, fg=theme.C_TEXT, bg=theme.C_BG,
        ).pack(side="left", anchor="center")

        pill_row = tk.Frame(hdr_outer, bg=theme.C_BG)
        pill_row.pack(side="right")

        for mode in ("Both", "Non-Empty", "Empty"):
            is_active = (self.filter_mode == mode)
            pill = ttk.Button(
                pill_row, text=mode,
                command=lambda m=mode: self.change_filter(m),
            )
            theme.apply_button_style(pill, primary=is_active)
            pill.pack(side="left", padx=3)

        # ── Pagination bar ──────────────────────────────────────────────────────
        total_pages = max(1, (len(self.image_list) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.current_page = max(0, min(self.current_page, total_pages - 1))

        if total_pages > 1:
            nav_outer = tk.Frame(self._content_area, bg=theme.C_BG)
            nav_outer.pack(fill="x", padx=28, pady=(8, 0))

            btn_prev = ttk.Button(nav_outer, text="‹ Prev", command=lambda: self._change_page(-1))
            theme.apply_button_style(btn_prev, primary=False)
            if self.current_page == 0:
                btn_prev.state(["disabled"])
            btn_prev.pack(side="left")

            tk.Label(
                nav_outer, text=f"Page {self.current_page + 1} of {total_pages}",
                font=theme.FONT_BODY, fg=theme.C_TEXT, bg=theme.C_BG,
            ).pack(side="left", padx=12)

            btn_next = ttk.Button(nav_outer, text="Next ›", command=lambda: self._change_page(1))
            theme.apply_button_style(btn_next, primary=False)
            if self.current_page >= total_pages - 1:
                btn_next.state(["disabled"])
            btn_next.pack(side="left")

        theme.make_separator(self._content_area, theme.C_BORDER).pack(
            fill="x", padx=28, pady=(10, 0)
        )

        # ── Scrollable image grid ─────────────────────────────────────────────
        grid_outer = tk.Frame(self._content_area, bg=theme.C_BG)
        grid_outer.pack(fill="both", expand=True)

        canvas    = tk.Canvas(grid_outer, bg=theme.C_SURFACE2, highlightthickness=0)
        scrollbar = ttk.Scrollbar(grid_outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        scroll_frame = tk.Frame(canvas, bg=theme.C_SURFACE2)
        win_id = canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width))
        scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        # Invalidate any thumbnail loads still in flight for a previous page.
        self._grid_generation += 1
        generation = self._grid_generation
        self._grid_cell_labels = {}

        if not self.image_list:
            tk.Label(
                scroll_frame,
                text="No images match the current filter selection.",
                font=theme.FONT_BODY, bg=theme.C_SURFACE2, fg=theme.C_MUTED,
            ).pack(pady=40, padx=20)
            return

        # ── Build placeholder cells for the current page ───────────────────────
        start = self.current_page * PAGE_SIZE
        page_items = self.image_list[start:start + PAGE_SIZE]

        for local_idx, img_info in enumerate(page_items):
            global_idx = start + local_idx
            col = local_idx % GRID_COLS
            row = local_idx // GRID_COLS
            scroll_frame.columnconfigure(col, weight=1)

            cell = tk.Frame(
                scroll_frame, bg=theme.C_SURFACE2,
                highlightbackground=theme.C_BORDER, highlightthickness=1,
                cursor="hand2",
            )
            cell.grid(row=row, column=col, padx=10, pady=10, sticky="nsew")

            status_color = theme.C_USED if img_info["is_empty"] else theme.C_AVAIL
            tk.Frame(cell, bg=status_color, height=3).pack(fill="x")

            lbl_img = tk.Label(
                cell, text="Loading…", bg=theme.C_SURFACE2, fg=theme.C_MUTED,
                font=theme.FONT_SMALL, cursor="hand2",
                width=THUMB[0] // 7, height=THUMB[1] // 16,
            )
            lbl_img.pack(padx=5, pady=(5, 3))
            lbl_img.bind("<Button-1>", lambda e, i=global_idx: self.open_image_viewer(i))
            self._grid_cell_labels[local_idx] = lbl_img

            info = tk.Frame(cell, bg=theme.C_SURFACE2)
            info.pack(padx=8, pady=(0, 8), fill="x")

            tk.Label(
                info, text=img_info["filename"],
                font=theme.FONT_SMALL, fg=theme.C_TEXT, bg=theme.C_SURFACE2,
                wraplength=145, justify="left",
            ).pack(anchor="w")

            status_text = "[EMPTY]" if img_info["is_empty"] else "[NON-EMPTY]"
            tk.Label(
                info, text=status_text,
                font=theme.FONT_MONO, fg=status_color, bg=theme.C_SURFACE2,
            ).pack(anchor="w", pady=(2, 0))

        threading.Thread(
            target=self._load_page_thumbnails,
            args=(generation, page_items),
            daemon=True,
        ).start()

    def _change_page(self, delta: int) -> None:
        self.current_page += delta
        self.show_image_grid()

    def _load_page_thumbnails(self, generation: int, page_items: list) -> None:
        """Background thread: decode + thumbnail each image on the current page."""
        for local_idx, img_info in enumerate(page_items):
            if generation != self._grid_generation:
                return   # user switched pages/folders; stop early

            try:
                img = Image.open(img_info["full_path"])
                img.thumbnail(THUMB)
            except Exception:
                img = None

            try:
                self.after(0, self._apply_thumbnail, generation, local_idx, img)
            except Exception:
                return   # window was closed mid-load

    def _apply_thumbnail(self, generation: int, local_idx: int, pil_img) -> None:
        """Main thread: convert a decoded thumbnail to PhotoImage and display it."""
        if generation != self._grid_generation:
            return   # stale result from a page the user has left

        lbl = self._grid_cell_labels.get(local_idx)
        if lbl is None or not lbl.winfo_exists():
            return

        if pil_img is None:
            lbl.config(text="Error Loading", image="", fg=theme.C_USED)
            return

        tk_img = ImageTk.PhotoImage(pil_img)
        lbl.config(image=tk_img, text="", width=0, height=0)
        lbl.image = tk_img

    def change_filter(self, new_mode: str) -> None:
        self.filter_mode = new_mode
        self.current_page = 0
        self.show_image_grid()

    # ── Step 5: Detail Carousel ───────────────────────────────────────────────

    def open_image_viewer(self, index: int) -> None:
        self.current_view_index = index
        self._show_contours      = False
        self._current_flake_data = []
        self._clear_content()

        self.viewer_header = tk.Frame(self._content_area, bg=theme.C_BG)
        self.viewer_header.pack(fill="x", padx=28, pady=(18, 0))

        btn_close = ttk.Button(
            self.viewer_header, text="← Grid View",
            command=self.show_image_grid,
        )
        theme.apply_button_style(btn_close, primary=False)
        btn_close.pack(side="left")

        self.viewer_title = tk.Label(
            self.viewer_header, text="",
            font=theme.FONT_TITLE, fg=theme.C_TEXT, bg=theme.C_BG,
        )
        self.viewer_title.pack(side="left", padx=18)

        theme.make_separator(self._content_area, theme.C_BORDER).pack(
            fill="x", padx=28, pady=(10, 0)
        )

        self.toolbar = tk.Frame(self._content_area, bg=theme.C_BG)
        self.toolbar.pack(fill="x", padx=28, pady=(10, 8))

        self.btn_toggle_empty = ttk.Button(
            self.toolbar, command=self.toggle_current_image_status,
        )
        theme.apply_button_style(self.btn_toggle_empty, primary=True)
        self.btn_toggle_empty.pack(side="left", padx=(0, 6))

        self.btn_flake = ttk.Button(
            self.toolbar, text="Add Flakes",
            command=self.action_add_flakes,
        )
        theme.apply_button_style(self.btn_flake, primary=True)
        self.btn_flake.pack(side="left", padx=(0, 6))

        self.btn_contours = ttk.Button(
            self.toolbar, text="Show Contours",
            command=self.toggle_contours,
        )
        theme.apply_button_style(self.btn_contours, primary=False)
        self.btn_contours.pack(side="left", padx=(0, 6))

        self._ensure_danger_style()
        self.btn_delete = ttk.Button(
            self.toolbar, text="Delete Image",
            style="Danger.TButton",
            command=self.delete_current_image,
        )
        self.btn_delete.pack(side="right")

        self.display_canvas = tk.Label(
            self._content_area,
            bg=theme.C_SURFACE2,
            highlightbackground=theme.C_BORDER, highlightthickness=1,
        )
        self.display_canvas.pack(fill="both", expand=True, padx=28, pady=(0, 16))
        self.display_canvas.bind("<Button-1>", self._on_viewer_click)

        self.bind("<Left>",  lambda e: self.navigate_carousel(-1))
        self.bind("<Right>", lambda e: self.navigate_carousel(1))

        self.render_carousel_image()

    def _ensure_danger_style(self) -> None:
        """Lazily register the Danger.TButton TTK style once."""
        style = ttk.Style()
        style.configure(
            "Danger.TButton",
            background=theme.C_USED, foreground="white",
            font=theme.FONT_BODY, relief="flat", borderwidth=0, focuscolor="none",
        )
        style.map(
            "Danger.TButton",
            background=[("active", "#8B1A1A"), ("pressed", "#8B1A1A")],
            foreground=[("active", "white"),   ("pressed", "white")],
        )

    def render_carousel_image(self) -> None:
        if self.current_view_index is None or not self.image_list:
            self.show_image_grid()
            return

        img_info     = self.image_list[self.current_view_index]
        status_label = "Empty" if img_info["is_empty"] else "Non-Empty"
        self.viewer_title.config(
            text=(
                f"{img_info['filename']}  ({status_label})"
                f"  —  [{self.current_view_index + 1} / {len(self.image_list)}]"
            )
        )

        if img_info["is_empty"]:
            self.btn_toggle_empty.config(text="Mark Non-Empty")
            self.btn_flake.pack_forget()
        else:
            self.btn_toggle_empty.config(text="Mark Empty")
            self.btn_flake.pack(side="left", padx=(0, 6), before=self.btn_delete)

        self.btn_contours.config(text="Hide Contours" if self._show_contours else "Show Contours")

        self._current_flake_data = []
        self.display_canvas.config(image="", text="Loading…")

        self._viewer_generation += 1
        generation     = self._viewer_generation
        show_contours  = self._show_contours

        threading.Thread(
            target=self._build_carousel_image,
            args=(generation, img_info, show_contours),
            daemon=True,
        ).start()

    def _build_carousel_image(self, generation: int, img_info: dict, show_contours: bool) -> None:
        """Background thread: decode the full image, optionally drawing flake
        contours onto it, then hand the result back to the main thread."""
        try:
            raw = Image.open(img_info["full_path"])
            orig_size = raw.size
            raw.thumbnail((1000, 500))
            disp_size = raw.size
            scale = disp_size[0] / orig_size[0] if orig_size[0] else 1.0

            flake_data: list[dict] = []
            if show_contours:
                raw = raw.convert("RGB")
                draw = ImageDraw.Draw(raw)
                folder = os.path.dirname(img_info["full_path"])
                json_path, flakes = self._load_flake_records(img_info)

                for flake in flakes:
                    flake_id = flake.get("flake_id")
                    npy_path = self._flake_npy_path(folder, flake_id)
                    if not npy_path:
                        continue
                    try:
                        pts = np.load(npy_path).reshape(-1, 2)
                    except Exception:
                        continue
                    scaled = [(int(x * scale), int(y * scale)) for x, y in pts]
                    if len(scaled) >= 2:
                        draw.line(scaled + [scaled[0]], fill=CONTOUR_COLOR, width=CONTOUR_WIDTH)
                    flake_data.append({
                        "flake_id":  flake_id,
                        "points":    pts,
                        "json_path": json_path,
                        "npy_path":  npy_path,
                    })

            self.after(0, self._apply_carousel_image, generation, raw, disp_size, scale, flake_data, None)
        except Exception as exc:
            try:
                self.after(0, self._apply_carousel_image, generation, None, None, None, [], exc)
            except Exception:
                pass

    def _apply_carousel_image(self, generation: int, pil_img, disp_size, scale, flake_data: list, exc) -> None:
        """Main thread: display the decoded image and remember contour data
        for click-to-delete hit testing."""
        if generation != self._viewer_generation:
            return   # user navigated away before this finished

        if exc is not None or pil_img is None:
            self.display_canvas.config(text=f"Failed to render image: {exc}", image="")
            return

        tk_img = ImageTk.PhotoImage(pil_img)
        self.display_canvas.config(image=tk_img, text="")
        self.display_canvas.image = tk_img

        self._img_scale         = scale
        self._img_disp_size     = disp_size
        self._current_flake_data = flake_data

    def toggle_contours(self) -> None:
        self._show_contours = not self._show_contours
        self.render_carousel_image()

    def _on_viewer_click(self, event) -> None:
        """When contours are visible, check if the click landed inside a
        flake's contour and, if so, offer to delete that flake."""
        if not self._show_contours or not self._current_flake_data:
            return

        label_w, label_h = self.display_canvas.winfo_width(), self.display_canvas.winfo_height()
        disp_w, disp_h    = self._img_disp_size
        offset_x = max((label_w - disp_w) // 2, 0)
        offset_y = max((label_h - disp_h) // 2, 0)

        if self._img_scale <= 0:
            return

        img_x = (event.x - offset_x) / self._img_scale
        img_y = (event.y - offset_y) / self._img_scale

        for flake in self._current_flake_data:
            if _point_in_polygon(img_x, img_y, flake["points"]):
                self._confirm_delete_flake(flake)
                return

    def _confirm_delete_flake(self, flake: dict) -> None:
        flake_id = flake.get("flake_id", "?")
        if not messagebox.askyesno(
            "Delete Flake?",
            f"Delete flake '{flake_id}'?\n\n"
            "This permanently removes its contour file and JSON entry.",
            parent=self,
        ):
            return

        npy_path = flake.get("npy_path")
        if npy_path:
            try:
                os.remove(npy_path)
            except OSError:
                pass

        json_path = flake.get("json_path")
        try:
            with open(json_path, "r") as f:
                data = json.load(f)
            data = [e for e in data if e.get("flake_id") != flake_id]
            if data:
                with open(json_path, "w") as f:
                    json.dump(data, f, indent=4)
            else:
                os.remove(json_path)
        except (OSError, json.JSONDecodeError) as exc:
            messagebox.showerror("File IO Error", f"Could not update JSON: {exc}", parent=self)
            return

        self.render_carousel_image()

    def navigate_carousel(self, direction: int) -> None:
        if not self.image_list:
            return
        self.current_view_index = (self.current_view_index + direction) % len(self.image_list)
        self.render_carousel_image()

    # ── Actions ───────────────────────────────────────────────────────────────

    def _get_flake_files(self, img_info: dict) -> tuple[str | None, list[str]]:
        """
        Returns (json_path_or_None, [npy_path, ...]) for the given image.

        Naming conventions:
        image : MMDDYY_chipnum_(row,col)_zoom.ext        e.g. 060626_0003_(-1.381, -0.104)_20.png
        json  : MMDDYY_chipnum_(row,col)_zoom.json       (same stem, same folder)
        npy   : chipnum_(row,col)_<id>_contour.npy       e.g. 0003_(-1.381, -0.104)_000_contour.npy
        """
        folder = os.path.dirname(img_info["full_path"])
        stem   = os.path.splitext(img_info["filename"])[0]

        # ── json: same stem, same folder ──────────────────────────────────────
        json_candidate = os.path.join(folder, stem + ".json")
        json_path = json_candidate if os.path.exists(json_candidate) else None
        
        npy_files: list[str] = []
        parts = stem.split("_", 1)
        if len(parts) == 2:
            without_date = parts[1]
            rc_match = re.match(r"^(.+_\([^)]+\))_", without_date)
            if rc_match:
                prefix = rc_match.group(1)                   # "0003_(-1.381, -0.104)"
                for fname in os.listdir(folder):
                    if fname.endswith("_contour.npy") and fname.startswith(prefix + "_"):
                        npy_files.append(os.path.join(folder, fname))

        return json_path, npy_files

    def _load_flake_records(self, img_info: dict) -> tuple[str, list[dict]]:
        """
        Returns (json_path, [flake_dict, ...]) for the given image's JSON
        sidecar. json_path is returned even if the file doesn't exist yet
        (caller can use it as a save target); the list is empty if missing
        or unreadable.
        """
        folder = os.path.dirname(img_info["full_path"])
        stem   = os.path.splitext(img_info["filename"])[0]
        json_path = os.path.join(folder, stem + ".json")

        if os.path.exists(json_path):
            try:
                with open(json_path, "r") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return json_path, data
            except (OSError, json.JSONDecodeError):
                pass

        return json_path, []

    def _flake_npy_path(self, folder: str, flake_id: str) -> str | None:
        """Returns the .npy contour path for flake_id, or None if not found."""
        if not flake_id:
            return None

        exact = os.path.join(folder, f"{flake_id}_contour.npy")
        if os.path.exists(exact):
            return exact

        for fname in os.listdir(folder):
            if fname.startswith(flake_id) and fname.endswith(".npy"):
                return os.path.join(folder, fname)

        return None

    def _delete_flake_files(self, img_info: dict) -> None:
        """Silently deletes the .json and all .npy flake files for img_info."""
        json_path, npy_files = self._get_flake_files(img_info)
        for path in ([json_path] if json_path else []) + npy_files:
            try:
                os.remove(path)
            except Exception:
                pass

    def _remove_dir_if_empty(self, directory: str) -> None:
        """Removes directory (not recursively) if it exists and is empty."""
        try:
            if os.path.isdir(directory) and not os.listdir(directory):
                os.rmdir(directory)
        except Exception:
            pass

    def toggle_current_image_status(self) -> None:
        img_info      = self.image_list[self.current_view_index]
        session_path  = os.path.join(IMAGES_DIR, self.current_session)
        marking_empty = not img_info["is_empty"]   # True  →  non-empty becomes empty

        # ── Confirm flake data deletion when marking as empty ─────────────────
        if marking_empty:
            json_path, npy_files = self._get_flake_files(img_info)
            if json_path or npy_files:
                if not messagebox.askyesno(
                    "Delete Flake Data?",
                    "This image has annotated flake data.\n\n"
                    "Marking it as empty will permanently delete the associated "
                    ".json and .npy files. Continue?",
                    parent=self,
                ):
                    return
                self._delete_flake_files(img_info)

        # ── Determine destination folder ──────────────────────────────────────
        if img_info["is_empty"]:
            target_parent = os.path.join(session_path, self.current_date, self.current_chip)
        else:
            target_parent = os.path.join(session_path, f"{self.current_date}_empty", self.current_chip)

        os.makedirs(target_parent, exist_ok=True)
        new_path = os.path.join(target_parent, img_info["filename"])

        try:
            source_dir = os.path.dirname(img_info["full_path"])
            shutil.move(img_info["full_path"], new_path)
            self._remove_dir_if_empty(source_dir)   # clean up if now empty

            img_info["full_path"] = new_path
            img_info["is_empty"]  = not img_info["is_empty"]

            if self.filter_mode != "Both":
                self.image_list.pop(self.current_view_index)
                if not self.image_list:
                    self.current_view_index = None
                elif self.current_view_index >= len(self.image_list):
                    self.current_view_index = 0

            self.render_carousel_image()
        except Exception as exc:
            messagebox.showerror("File IO Error", f"Could not move file: {exc}", parent=self)

    def _get_scan_date_from_filepath(self, file_path):
        match = re.search(r"ScanningSession_(\d{2})(\d{2})(\d{2})", file_path)
        if match:
            mm, dd, yy = match.groups()
            # Rearrange into YYYY-MM-DD (assuming 20xx century)
            formatted_date = f"20{yy}-{mm}-{dd}"
            
            return formatted_date
        return ""

    def action_add_flakes(self) -> None:
            img_info = self.image_list[self.current_view_index]
            abs_path = img_info['full_path']
            scan_date = self._get_scan_date_from_filepath(abs_path)
            open_annotator(self, img_info, date_scanned=scan_date)

    def delete_current_image(self) -> None:
        if self.current_view_index is None or not self.image_list:
            return

        img_info = self.image_list[self.current_view_index]
        
        if not messagebox.askyesno(
            "Confirm Deletion", 
            f"Permanently delete this image and all associated flake data?\n\nFile: {img_info['filename']}", 
            parent=self,
        ):
            return

        try:
            # 1. Silently get rid of the associated contours and json files
            self._delete_flake_files(img_info)

            # 2. Delete the primary image file
            if os.path.exists(img_info["full_path"]):
                os.remove(img_info["full_path"])

            # 3. Clean up parent directory if it's now completely empty
            source_dir = os.path.dirname(img_info["full_path"])
            self._remove_dir_if_empty(source_dir)

            # 4. Remove item from the in-memory pagination list
            self.image_list.pop(self.current_view_index)

            # 5. Handle carousel index shifting or drop back to grid view
            if not self.image_list:
                self.current_view_index = None
                self.show_image_grid()
            else:
                if self.current_view_index >= len(self.image_list):
                    self.current_view_index = 0
                self.render_carousel_image()

        except Exception as exc:
            messagebox.showerror("File IO Error", f"Could not complete deletion: {exc}", parent=self)


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = ImageBrowserApp()
    app.mainloop()