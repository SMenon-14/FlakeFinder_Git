import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk

from src.browser.constants import (
    C_BG, C_SURFACE, C_SURFACE2, C_ACCENT, C_TEXT, C_MUTED, C_BORDER,
    C_USED, C_AVAIL, C_ROW_ALT,
    FONT_BODY, FONT_SMALL, FONT_TITLE, FONT_HEAD,
    IMAGE_THUMB, CARD_IMG,
)
from src.browser.database import FlakeDatabase
from src.browser.widgets import StyledButton, LabeledField, StatusBadge


class SearchPage(tk.Frame):

    SORT_FIELDS = ["Material", "Layers", "Max Size (µm)",
                   "Date Exfoliated", "Date Scanned", "Usage Status", "Session", "Chip", "User"]

    ROW_BATCH = 60  # rows/cards built per idle tick, so huge result sets don't freeze the UI
    PAGE_SIZE = 100  # results rendered at once; each row/card is several native
                     # widgets, and Windows has a hard cap on window handles

    def __init__(self, parent, db: FlakeDatabase, on_select_flake, on_close=None):
        super().__init__(parent, bg=C_BG)
        self.db              = db
        self.on_select_flake = on_select_flake
        self.on_close = on_close
        self._grid_view      = False
        self._adv_open       = False
        self._sort_asc       = True
        self._results        = []
        self._page_results   = []
        self._page           = 0
        self._thumb_cache    = {}
        self._thumb_queue    = []
        self._thumb_token    = 0

        self._top_section = tk.Frame(self, bg=C_BG)
        self._top_section.pack(fill="x", side="top")

        self._build_toolbar()
        self._build_advanced_panel()
        self._build_results_area()
        self.run_search()

    # ── Toolbar ───────────────────────────────────────────────────────────────

    def _build_toolbar(self):
        bar = tk.Frame(self._top_section, bg=C_BG, pady=8, padx=10)
        bar.pack(fill="x")

        StyledButton(bar, "Browse", active=True,
                    command=self.run_search).pack(side="left", padx=(0, 8))

        self._btn_adv = StyledButton(bar, "Advanced Search",
                                    command=self._toggle_advanced)
        self._btn_adv.pack(side="left", padx=(0, 12))

        self._search_var = tk.StringVar()
        sf = tk.Frame(bar, bg=C_SURFACE2, padx=6, pady=3)
        sf.pack(side="left")                          # ← no fill/expand
        tk.Label(sf, text="🔍", bg=C_SURFACE2, fg=C_MUTED).pack(side="left")
        tk.Entry(sf, textvariable=self._search_var,
                bg=C_SURFACE2, fg=C_TEXT, insertbackground=C_TEXT,
                relief="flat", font=FONT_BODY,
                width=20,                            # ← fixed narrower width
                highlightthickness=0).pack(side="left")
        self._search_var.trace_add("write", lambda *_: self.run_search())

        tk.Label(bar, text="Sort By", bg=C_BG, fg=C_MUTED,
                font=FONT_BODY).pack(side="left", padx=(12, 4))
        self._sort_var = tk.StringVar(value="")
        sort_cb = ttk.Combobox(bar, textvariable=self._sort_var,
                            values=[""] + self.SORT_FIELDS,
                            state="readonly", width=14, font=FONT_BODY)
        sort_cb.pack(side="left")
        sort_cb.bind("<<ComboboxSelected>>", lambda _: self.run_search())

        self._btn_sort_dir = StyledButton(bar, "↑ Asc",
                                        command=self._toggle_sort_dir)
        self._btn_sort_dir.pack(side="left", padx=4)

        self._btn_grid = StyledButton(bar, "⊞", command=self._set_grid)
        self._btn_list = StyledButton(bar, "☰", command=self._set_list, active=True)
        self._btn_grid.pack(side="left", padx=2)     # ← side="left" now
        self._btn_list.pack(side="left", padx=2)     # ← side="left" now

        # Spacer pushes "Back to Home" to the far right
        tk.Frame(bar, bg=C_BG).pack(side="left", fill="x", expand=True)

        lbl_close = tk.Label(
            bar, text="✕  Close",
            font=FONT_SMALL, fg=C_USED, bg=C_BG, cursor="hand2",
        )
        lbl_close.pack(side="right", padx=(8, 0))
        lbl_close.bind("<Enter>",    lambda e: lbl_close.config(fg=C_TEXT))
        lbl_close.bind("<Leave>",    lambda e: lbl_close.config(fg=C_MUTED))
        lbl_close.bind("<Button-1>", lambda e: self._close())

        # Pagination controls, shown/hidden on demand by _update_page_bar
        self._page_bar = tk.Frame(bar, bg=C_BG)
        self._btn_prev_page = StyledButton(self._page_bar, "← Prev",
                                            command=lambda: self._go_page(-1))
        self._btn_prev_page.pack(side="left")
        self._page_label = tk.Label(self._page_bar, text="", bg=C_BG,
                                     fg=C_MUTED, font=FONT_BODY)
        self._page_label.pack(side="left", padx=12)
        self._btn_next_page = StyledButton(self._page_bar, "Next →",
                                            command=lambda: self._go_page(1))
        self._btn_next_page.pack(side="left")

    def _close(self):
        if self.on_close:
            self.on_close()


    def _toggle_sort_dir(self):
        self._sort_asc = not self._sort_asc
        self._btn_sort_dir.config(text="↑ Asc" if self._sort_asc else "↓ Desc")
        self.run_search()

    def _set_grid(self):
        self._grid_view = True
        self._btn_grid.set_active(True)
        self._btn_list.set_active(False)
        self._render_results()

    def _set_list(self):
        self._grid_view = False
        self._btn_list.set_active(True)
        self._btn_grid.set_active(False)
        self._render_results()

    # ── Advanced Panel ────────────────────────────────────────────────────────

    def _build_advanced_panel(self):
        self._adv_frame = tk.Frame(self._top_section, bg=C_BG, padx=10, pady=6)

        row1 = tk.Frame(self._adv_frame, bg=C_BG)
        row1.pack(fill="x", pady=(0, 6))

        # Material
        mf = tk.Frame(row1, bg=C_BG)
        mf.pack(side="left", padx=(0, 20))
        tk.Label(mf, text="Material", bg=C_BG, fg=C_MUTED,
                 font=FONT_HEAD).pack(anchor="w")
        self._mat_var   = tk.StringVar()
        self._mat_combo = ttk.Combobox(mf, textvariable=self._mat_var,
                                       values=[""] + self.db.materials,
                                       width=16, font=FONT_BODY)
        self._mat_combo.pack(side="left")

        # Max Size range
        sf2 = tk.Frame(row1, bg=C_BG)
        sf2.pack(side="left", padx=(0, 20))
        tk.Label(sf2, text="Max Size (µm)", bg=C_BG, fg=C_MUTED,
                 font=FONT_HEAD).pack(anchor="w")
        inner = tk.Frame(sf2, bg=C_BG)
        inner.pack()
        self._size_min = tk.StringVar()
        self._size_max = tk.StringVar()
        tk.Entry(inner, textvariable=self._size_min, bg=C_SURFACE2, fg=C_TEXT,
                 insertbackground=C_TEXT, relief="flat", font=FONT_BODY,
                 width=6).pack(side="left")
        tk.Label(inner, text=" to ", bg=C_BG, fg=C_MUTED,
                 font=FONT_BODY).pack(side="left")
        tk.Entry(inner, textvariable=self._size_max, bg=C_SURFACE2, fg=C_TEXT,
                 insertbackground=C_TEXT, relief="flat", font=FONT_BODY,
                 width=6).pack(side="left")

        # Usage Status
        uf = tk.Frame(row1, bg=C_BG)
        uf.pack(side="left")
        tk.Label(uf, text="Usage Status", bg=C_BG, fg=C_MUTED,
                 font=FONT_HEAD).pack(anchor="w")
        self._status_var = tk.StringVar()
        ttk.Combobox(uf, textvariable=self._status_var,
                     values=["", "Available", "Used"],
                     state="readonly", width=12, font=FONT_BODY).pack()

        row2 = tk.Frame(self._adv_frame, bg=C_BG)
        row2.pack(fill="x")

        urf = tk.Frame(row1, bg=C_BG)
        urf.pack(side="left", padx=(20, 0))
        tk.Label(urf, text="User", bg=C_BG, fg=C_MUTED,
                 font=FONT_HEAD).pack(anchor="w")
        self._user_var = tk.StringVar()
        self._user_entry = tk.Entry(urf, textvariable=self._user_var,
                                    bg=C_SURFACE2, fg=C_TEXT,
                                    insertbackground=C_TEXT, relief="flat",
                                    font=FONT_BODY, width=14)
        self._user_entry.pack()

        # Chip Number
        cnf = tk.Frame(row1, bg=C_BG)
        cnf.pack(side="left", padx=(20, 0))
        tk.Label(cnf, text="Chip Number", bg=C_BG, fg=C_MUTED,
                 font=FONT_HEAD).pack(anchor="w")
        self._chip_var = tk.StringVar()
        self._chip_entry = tk.Entry(cnf, textvariable=self._chip_var,
                                    bg=C_SURFACE2, fg=C_TEXT,
                                    insertbackground=C_TEXT, relief="flat",
                                    font=FONT_BODY, width=10)
        self._chip_entry.pack()

        # Layers range
        lf = tk.Frame(row2, bg=C_BG)
        lf.pack(side="left", padx=(0, 20))
        tk.Label(lf, text="Layers", bg=C_BG, fg=C_MUTED,
                 font=FONT_HEAD).pack(anchor="w")
        inner2 = tk.Frame(lf, bg=C_BG)
        inner2.pack()
        self._layers_min = tk.StringVar()
        self._layers_max = tk.StringVar()
        tk.Entry(inner2, textvariable=self._layers_min, bg=C_SURFACE2, fg=C_TEXT,
                 insertbackground=C_TEXT, relief="flat", font=FONT_BODY,
                 width=5).pack(side="left")
        tk.Label(inner2, text=" to ", bg=C_BG, fg=C_MUTED,
                 font=FONT_BODY).pack(side="left")
        tk.Entry(inner2, textvariable=self._layers_max, bg=C_SURFACE2, fg=C_TEXT,
                 insertbackground=C_TEXT, relief="flat", font=FONT_BODY,
                 width=5).pack(side="left")

        self._date_exf  = LabeledField(row2, "Date Exfoliated", width=12)
        self._date_exf.pack(side="left", padx=(0, 20))

        self._date_scan = LabeledField(row2, "Date Scanned", width=12)
        self._date_scan.pack(side="left", padx=(0, 20))

        StyledButton(row2, "Apply", command=self.run_search,
                     active=True).pack(side="left", anchor="s", pady=2)
        StyledButton(row2, "Clear",
                     command=self._clear_advanced).pack(side="left", anchor="s",
                                                        pady=2, padx=(6, 0))

    def _toggle_advanced(self):
        self._adv_open = not self._adv_open
        self._btn_adv.set_active(self._adv_open)
        if self._adv_open:
            self._adv_frame.pack(fill="x")
        else:
            self._adv_frame.pack_forget()

    def _clear_advanced(self):
        self._mat_var.set("")
        self._size_min.set("")
        self._size_max.set("")
        self._status_var.set("")
        self._layers_min.set("")
        self._layers_max.set("")
        self._user_var.set("")
        self._chip_var.set("")
        self._date_exf.clear()
        self._date_scan.clear()
        self.run_search()
    # ── Results Area ──────────────────────────────────────────────────────────

    def _build_results_area(self):
        container = tk.Frame(self, bg=C_BG)
        container.pack(fill="both", expand=True, padx=10, pady=(4, 10))

        self._canvas = tk.Canvas(container, bg=C_BG,
                                 highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical",
                                  command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self._inner = tk.Frame(self._canvas, bg=C_BG)
        self._canvas_window = self._canvas.create_window(
            (0, 0), window=self._inner, anchor="nw")

        self._inner.bind("<Configure>", self._on_inner_resize)
        self._canvas.bind("<Configure>", self._on_canvas_resize)
        self._canvas.bind("<MouseWheel>", self._on_scroll)
        self._canvas.bind("<Button-4>",   self._on_scroll)
        self._canvas.bind("<Button-5>",   self._on_scroll)

    def _on_inner_resize(self, _):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_resize(self, event):
        self._canvas.itemconfig(self._canvas_window, width=event.width)

    def _on_scroll(self, event):
        if event.num == 4:
            self._canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            self._canvas.yview_scroll(1, "units")
        else:
            self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # ── Search Execution ──────────────────────────────────────────────────────

    def run_search(self, *_):
        def _f(s):
            try:    return float(s.strip())
            except: return None

        def _i(s):
            try:    return int(s.strip())
            except: return None

        adv = self._adv_open
        self._results = self.db.search(
            text            = self._search_var.get(),
            material        = self._mat_var.get()        if adv else "",
            size_min        = _f(self._size_min.get())   if adv else None,
            size_max        = _f(self._size_max.get())   if adv else None,
            layers_min      = _i(self._layers_min.get()) if adv else None,
            layers_max      = _i(self._layers_max.get()) if adv else None,
            date_exfoliated = self._date_exf.get()       if adv else "",
            date_scanned    = self._date_scan.get()       if adv else "",
            usage_status    = self._status_var.get()     if adv else "",
            user            = self._user_var.get()       if adv else "",
            chip            = self._chip_var.get()       if adv else "",
            sort_field      = self._sort_var.get(),
            sort_asc        = self._sort_asc,
        )
        self._page = 0
        self._render_results()

    # ── Render ────────────────────────────────────────────────────────────────

    def _render_results(self):
        self._thumb_token += 1
        token = self._thumb_token

        for w in self._inner.winfo_children():
            w.destroy()
        self._thumb_cache = {}
        self._thumb_queue = []

        total = len(self._results)

        if not total:
            self._page_results = []
            self._update_page_bar(0, 0)
            tk.Label(self._inner, text="No flakes found.",
                     bg=C_BG, fg=C_MUTED, font=FONT_BODY,
                     pady=40).pack()
            return

        total_pages = (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE
        self._page  = max(0, min(self._page, total_pages - 1))

        start = self._page * self.PAGE_SIZE
        self._page_results = self._results[start:start + self.PAGE_SIZE]
        self._update_page_bar(total, total_pages)

        if self._grid_view:
            self.after_idle(self._render_grid_batch, 0, token)
        else:
            self._build_list_header()
            self.after_idle(self._render_list_batch, 0, token)

    def _update_page_bar(self, total, total_pages):
        if total_pages <= 1:
            self._page_bar.pack_forget()
            return

        start = self._page * self.PAGE_SIZE + 1
        end   = min(start + self.PAGE_SIZE - 1, total)
        self._page_label.config(
            text=f"Showing {start}-{end} of {total}  "
                 f"(page {self._page + 1} of {total_pages})")

        self._page_bar.pack(side="right", padx=(8, 0))

    def _go_page(self, delta):
        total_pages = (len(self._results) + self.PAGE_SIZE - 1) // self.PAGE_SIZE
        new_page = self._page + delta
        if 0 <= new_page < total_pages:
            self._page = new_page
            self._render_results()

    def _process_thumb_queue(self, token):
        # A newer render started; abandon this queue.
        if token != self._thumb_token or not self._thumb_queue:
            return
        lbl, path, size = self._thumb_queue.pop(0)
        if lbl.winfo_exists():
            photo = self._load_thumb(path, size)
            if photo:
                lbl.configure(image=photo)
                lbl.image = photo
            else:
                lbl.configure(text="No Image", fg=C_MUTED, font=FONT_SMALL)
        self.after_idle(self._process_thumb_queue, token)

    def _load_thumb(self, path: str, size: tuple) -> ImageTk.PhotoImage | None:
        key = (path, size)
        if key in self._thumb_cache:
            return self._thumb_cache[key]
        try:
            img = Image.open(path)
            img.thumbnail(size, Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._thumb_cache[key] = photo
            return photo
        except Exception:
            return None

    def _build_list_header(self):
        THUMB_W = 140
        ROW_H   = 76
        # cols: 0=thumb, 1=material, 2=layers, 3=max size, 4=date exf, 5=date scan, 6=status
        COL_FIXED = [80, 95, 130, 120, 105, 100]

        def _col_cfg(frame):
            frame.grid_columnconfigure(0, minsize=THUMB_W, weight=0)
            frame.grid_columnconfigure(1, weight=1)
            for ci, w in enumerate(COL_FIXED, start=2):
                frame.grid_columnconfigure(ci, minsize=w, weight=0)

        header = tk.Frame(self._inner, bg=C_SURFACE2)
        header.pack(fill="x", side="top")
        _col_cfg(header)
        headers = ["Image", "Material", "Layers", "Max Size (µm)",
                   "Date Exfoliated", "Date Scanned", "Usage Status", "User"]
        anchors = ["w", "w", "center", "center", "center", "center", "center", "w"]
        for ci, (txt, anc) in enumerate(zip(headers, anchors)):
            tk.Label(header, text=txt, bg=C_SURFACE2, fg=C_MUTED,
                     font=FONT_HEAD, anchor=anc, padx=8, pady=5
                     ).grid(row=0, column=ci, sticky="ew")

        self._list_layout = (THUMB_W, ROW_H, _col_cfg, anchors)

    def _render_list_batch(self, start, token):
        # A newer render started; abandon this batch.
        if token != self._thumb_token:
            return

        THUMB_W, ROW_H, _col_cfg, anchors = self._list_layout
        end = min(start + self.ROW_BATCH, len(self._page_results))

        for i in range(start, end):
            flake   = self._page_results[i]
            bg      = C_SURFACE if i % 2 == 0 else C_ROW_ALT
            used    = flake.get("used", False)
            fg_data = C_USED if used else C_AVAIL

            row = tk.Frame(self._inner, bg=bg, cursor="hand2", height=ROW_H)
            row.pack(fill="x", side="top")
            row.pack_propagate(False)
            _col_cfg(row)

            # Thumbnail (decoded asynchronously so the UI stays responsive)
            thumb_cell = tk.Frame(row, bg=bg, width=THUMB_W, height=ROW_H)
            thumb_cell.grid(row=0, column=0, sticky="nsew")
            thumb_cell.grid_propagate(False)
            thumb_lbl = tk.Label(thumb_cell, bg=bg)
            thumb_lbl.place(relx=0.5, rely=0.5, anchor="center")
            self._thumb_queue.append(
                (thumb_lbl, flake.get("_image_path", ""), IMAGE_THUMB))

            # Text cells
            max_size = flake.get("max_sidelength_um")
            size_str = f"{max_size:.1f}" if max_size is not None else "—"
            values = [
                flake.get("material",        "—"),
                str(flake.get("layers",      "—")),
                size_str,
                str(flake.get("date_exfoliated", "—")),
                str(flake.get("date_scanned",    "—")),
                "Used" if used else "Available",
                str(flake.get("user",        "—")),
            ]
            for ci, (val, anc) in enumerate(zip(values, anchors[1:]), start=1):
                lbl = tk.Label(row, text=val, bg=bg, fg=fg_data,
                               font=FONT_BODY, anchor=anc, padx=8)
                lbl.grid(row=0, column=ci, sticky="ew")
                lbl.bind("<Button-1>", lambda e, f=flake: self.on_select_flake(f))

            for widget in (row, thumb_cell):
                widget.bind("<Button-1>", lambda e, f=flake: self.on_select_flake(f))

        if end < len(self._page_results):
            self.after_idle(self._render_list_batch, end, token)
        elif self._thumb_queue:
            self.after_idle(self._process_thumb_queue, token)

    def _render_grid_batch(self, start, token):
        # A newer render started; abandon this batch.
        if token != self._thumb_token:
            return

        COLS = 4
        end = min(start + self.ROW_BATCH, len(self._page_results))

        for i in range(start, end):
            flake = self._page_results[i]
            r, c = divmod(i, COLS)

            card = tk.Frame(self._inner, bg=C_SURFACE, padx=6, pady=6,
                            cursor="hand2",
                            highlightbackground=C_BORDER,
                            highlightthickness=1)
            card.grid(row=r, column=c, padx=6, pady=6, sticky="nsew")
            self._inner.columnconfigure(c, weight=1)
            card.bind("<Button-1>", lambda e, f=flake: self.on_select_flake(f))

            img_frame = tk.Frame(card, bg=C_SURFACE,
                                  width=CARD_IMG[0], height=CARD_IMG[1])
            img_frame.pack()
            img_frame.pack_propagate(False)
            thumb_lbl = tk.Label(img_frame, bg=C_SURFACE)
            thumb_lbl.place(relx=0.5, rely=0.5, anchor="center")
            thumb_lbl.bind("<Button-1>", lambda e, f=flake: self.on_select_flake(f))
            self._thumb_queue.append(
                (thumb_lbl, flake.get("_image_path", ""), CARD_IMG))

            max_size = flake.get("max_sidelength_um")
            size_str = f"{max_size:.1f} µm" if max_size is not None else "—"

            tk.Label(card, text=flake.get("material", "—"),
                     bg=C_SURFACE, fg=C_TEXT, font=FONT_TITLE).pack(anchor="w")
            tk.Label(card,
                     text=f"{flake.get('layers', '—')} layers  •  {size_str}",
                     bg=C_SURFACE, fg=C_MUTED, font=FONT_SMALL).pack(anchor="w")
            StatusBadge(card, flake.get("used", False)).pack(anchor="w", pady=(4, 0))

        if end < len(self._page_results):
            self.after_idle(self._render_grid_batch, end, token)
        elif self._thumb_queue:
            self.after_idle(self._process_thumb_queue, token)
