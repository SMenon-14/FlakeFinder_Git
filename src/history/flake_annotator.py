"""
flake_annotator.py
──────────────────
Launched from scan_history.action_add_flakes().
Opens a Toplevel window for annotating flakes on a scan image:
  - Freehand polygon drawing on a zoomable canvas
  - Per-flake metadata form (material, substrate, layers, thickness)
  - Computes min/max sidelength via px_to_um() placeholder
  - Saves / appends to a JSON sidecar next to the image
  - Saves each flake contour as a .npy file next to the image

Filename convention expected:
    MMDDYY_chipnum_(row, col)_zoom.png
    e.g.  060526_4_(2, 8)_5x.png
"""

from __future__ import annotations

import json
import os
import re
import math
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional

import numpy as np
from PIL import Image, ImageTk

# ── Import project theme (same pattern as image_browser.py) ──────────────────
import src.constants.themes.theme as theme
from src.paths import CONFIGS_DIR
DEFAULT_CALIBRATION_PATH = CONFIGS_DIR / 'calibrated_scale_values.json'

def __load_scale_factor(zoom):
        with open(DEFAULT_CALIBRATION_PATH, 'r') as file:
            data = json.load(file)
        return data['zoom_levels'][zoom]


def px_to_um(pixels: float, zoom: str = "") -> float:
    """
    Convert a pixel length to microns.

    Replace this body with your real implementation.
    `zoom` is the zoom string extracted from the filename (e.g. "5x", "10x").
    """
    scale_factor = __load_scale_factor(zoom)
    return scale_factor*pixels

# ──────────────────────────────────────────────────────────────────────────────
#  Filename parser
# ──────────────────────────────────────────────────────────────────────────────

def parse_filename(fname: str) -> dict:
    """Return dict with date_exfoliated, chip, row, col, zoom from filename."""
    filename_no_ext, ext = os.path.splitext(os.path.basename(fname))
    result = filename_no_ext.split("_")
    d = result[0]
    chip = result[1]
    coords = result[2]
    row, col = coords.strip("()").split(", ")
    zoom = result[3]
    return {
        "date_exfoliated": f"20{d[4:6]}-{d[0:2]}-{d[2:4]}",
        "chip": chip,
        "row":   row,
        "col":    col,
        "zoom": zoom,
    }


# ──────────────────────────────────────────────────────────────────────────────
#  Geometry helpers
# ──────────────────────────────────────────────────────────────────────────────
def _polygon_bbox(pts: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)

def _polygon_sidelengths(pts: list[tuple[float, float]]) -> tuple[float, float]:
    """Return (max_side_px, min_side_px) using bounding-box diagonal approach."""
    x0, y0, x1, y1 = _polygon_bbox(pts)
    w, h = x1 - x0, y1 - y0
    diag = math.hypot(w, h)
    short = min(w, h)
    return diag, short

def _polygon_center(pts: list[tuple[float, float]]) -> tuple[int, int]:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return int(sum(xs) / len(xs)), int(sum(ys) / len(ys))


# ──────────────────────────────────────────────────────────────────────────────
#  JSON helpers
# ──────────────────────────────────────────────────────────────────────────────
def _load_json(path: str) -> list:
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return []

def _save_json(path: str, data: list) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=4)

def _next_flake_index(existing: list, row: int, col: int) -> int:
    """Return the next sequential index for flakes sharing the same (row, col)."""
    rc_tag = f"({row}, {col})"
    count = sum(1 for e in existing if e.get("(row, col)") == rc_tag)
    return count


# ──────────────────────────────────────────────────────────────────────────────
#  Main annotator window
# ──────────────────────────────────────────────────────────────────────────────
class FlakeAnnotator(tk.Toplevel):
    """
    Modal-ish Toplevel that lets the user draw polygon flake contours and
    save metadata alongside the source image.

    Parameters
    ----------
    parent      : tk.Tk or tk.Toplevel
    image_info  : dict with keys 'full_path', 'filename', 'is_empty'
    date_scanned: 'YYYY-MM-DD' string (today / scan date from app context)
    """

    POINT_R  = 4      # radius of drawn polygon vertices
    LINE_W   = 2      # polygon edge width
    POLY_COL = "#00E5FF"
    DONE_COL = "#69FF47"
    VERT_COL = "#FF5252"

    def __init__(
        self,
        parent,
        image_info: dict,
        date_scanned: str = "",
    ) -> None:
        super().__init__(parent)
        self.transient(parent)
        self.grab_set()

        self.image_info  = image_info
        self.parsed      = parse_filename(image_info["filename"])
        self.date_scanned = date_scanned or _today_iso()
        self.zoom_str    = self.parsed.get("zoom", "")

        # JSON sidecar lives next to the image
        img_dir   = os.path.dirname(image_info["full_path"])
        stem      = os.path.splitext(image_info["filename"])[0]
        self.json_path = os.path.join(img_dir, f"{stem}.json")
        self.img_dir   = img_dir
        self.stem      = stem

        # Canvas drawing state
        self._raw_image: Optional[Image.Image] = None
        self._tk_image:  Optional[ImageTk.PhotoImage] = None
        self._scale     = 1.0   # display scale vs original
        self._points: list[tuple[float, float]] = []   # canvas coords
        self._poly_items: list[int] = []   # canvas item ids
        self._drawing   = False

        # Saved flakes this session (for sidebar list)
        self._session_flakes: list[dict] = []

        self.title(f"Annotate Flakes — {image_info['filename']}")
        self.configure(bg=theme.C_BG)
        theme.setup_ttk_styles()

        sw = parent.winfo_screenwidth()
        sh = parent.winfo_screenheight()
        w, h = min(1400, sw - 40), min(860, sh - 60)
        self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        self.minsize(900, 600)

        self._build_ui()
        self._load_image()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Accent bar
        tk.Frame(self, bg=theme.C_ACCENT, height=3).pack(fill="x")

        # Top bar
        topbar = tk.Frame(
            self, bg=theme.C_SURFACE,
            highlightbackground=theme.C_BORDER, highlightthickness=1, height=48,
        )
        topbar.pack(fill="x")
        topbar.pack_propagate(False)

        tk.Label(
            topbar, text=f"Flake Annotator  —  {self.image_info['filename']}",
            font=("Segoe UI Semibold", 12), fg=theme.C_TEXT, bg=theme.C_SURFACE,
        ).pack(side="left", padx=18)

        # Parsed info badges
        badge_frame = tk.Frame(topbar, bg=theme.C_SURFACE)
        badge_frame.pack(side="left", padx=10)
        for label, val in [
            ("Date exfoliated", self.parsed.get("date_exfoliated", "?")),
            ("Chip",            self.parsed.get("chip", "?")),
            ("Row,Col",         f"({self.parsed.get('row','?')}, {self.parsed.get('col','?')})"),
            ("Zoom",            self.parsed.get("zoom", "?")),
        ]:
            b = tk.Frame(badge_frame, bg=theme.C_SURFACE2,
                         highlightbackground=theme.C_BORDER, highlightthickness=1)
            b.pack(side="left", padx=4)
            tk.Label(b, text=label, font=theme.FONT_SMALL,
                     fg=theme.C_MUTED, bg=theme.C_SURFACE2).pack(side="left", padx=(6,2), pady=3)
            tk.Label(b, text=val, font=("Segoe UI Semibold", 10),
                     fg=theme.C_TEXT, bg=theme.C_SURFACE2).pack(side="left", padx=(0,6), pady=3)

        btn_close = ttk.Button(topbar, text="✕  Close", command=self.destroy)
        theme.apply_button_style(btn_close, primary=False)
        btn_close.pack(side="right", padx=12)

        # Body: canvas (left) + sidebar (right)
        body = tk.Frame(self, bg=theme.C_BG)
        body.pack(fill="both", expand=True)

        # ── Canvas area ───────────────────────────────────────────────────────
        canvas_frame = tk.Frame(body, bg=theme.C_SURFACE2,
                                highlightbackground=theme.C_BORDER, highlightthickness=1)
        canvas_frame.pack(side="left", fill="both", expand=True, padx=(12,0), pady=12)

        self.canvas = tk.Canvas(
            canvas_frame, bg="#1A1A2E", cursor="crosshair",
            highlightthickness=0,
        )
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<Button-1>",        self._on_click)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<Button-3>",        self._on_right_click)
        self.canvas.bind("<Motion>",          self._on_motion)
        self.bind("<Escape>",                 lambda e: self._cancel_polygon())
        self.bind("<Return>",                 lambda e: self._finish_polygon())

        # Instructions overlay
        self._hint_var = tk.StringVar(value="Left-click to add points  |  Double-click or Enter to finish  |  Right-click or Esc to cancel")
        tk.Label(
            canvas_frame, textvariable=self._hint_var,
            font=theme.FONT_SMALL, fg=theme.C_MUTED, bg="#1A1A2E",
        ).pack(side="bottom", pady=4)

        # Rubber-band line (preview segment while drawing)
        self._rubber_line: Optional[int] = None

        # ── Sidebar ───────────────────────────────────────────────────────────
        sidebar = tk.Frame(body, bg=theme.C_BG, width=300)
        sidebar.pack(side="right", fill="y", padx=12, pady=12)
        sidebar.pack_propagate(False)

        # ── Metadata form ─────────────────────────────────────────────────────
        tk.Label(sidebar, text="Flake Metadata",
                 font=theme.FONT_TITLE, fg=theme.C_TEXT, bg=theme.C_BG).pack(anchor="w")
        theme.make_separator(sidebar, theme.C_BORDER).pack(fill="x", pady=(4, 10))

        form = tk.Frame(sidebar, bg=theme.C_BG)
        form.pack(fill="x")

        def _field(label_text, row, default="", width=22):
            tk.Label(form, text=label_text, font=theme.FONT_SMALL,
                     fg=theme.C_MUTED, bg=theme.C_BG).grid(
                row=row, column=0, sticky="w", pady=3, padx=(0,8))
            var = tk.StringVar(value=default)
            ent = ttk.Entry(form, textvariable=var, width=width,
                            font=theme.FONT_BODY)
            ent.grid(row=row, column=1, sticky="ew", pady=3)
            form.columnconfigure(1, weight=1)
            return var

        self.v_material   = _field("Material",         0, "Graphene")
        self.v_substrate  = _field("Substrate",        1, "SiO2_90nm")
        self.v_thickness  = _field("Thickness (nm)",   2, "0.335")
        self.v_layers     = _field("Layers",           3, "1")
        self.v_date_scan  = _field("Date scanned",     4, self.date_scanned)
        self.v_user       = _field("User",             5, "")

        theme.make_separator(sidebar, theme.C_BORDER).pack(fill="x", pady=(14, 8))

        # ── Draw controls ─────────────────────────────────────────────────────
        tk.Label(sidebar, text="Drawing Controls",
                 font=theme.FONT_TITLE, fg=theme.C_TEXT, bg=theme.C_BG).pack(anchor="w")

        ctrl = tk.Frame(sidebar, bg=theme.C_BG)
        ctrl.pack(fill="x", pady=(6, 0))

        self.btn_finish = ttk.Button(ctrl, text="✓  Finish Polygon",
                                     command=self._finish_polygon)
        theme.apply_button_style(self.btn_finish, primary=True)
        self.btn_finish.pack(fill="x", pady=(0, 4))

        self.btn_cancel = ttk.Button(ctrl, text="✕  Cancel Polygon",
                                     command=self._cancel_polygon)
        theme.apply_button_style(self.btn_cancel, primary=False)
        self.btn_cancel.pack(fill="x", pady=(0, 4))

        self.btn_undo = ttk.Button(ctrl, text="⌫  Undo Last Point",
                                   command=self._undo_point)
        theme.apply_button_style(self.btn_undo, primary=False)
        self.btn_undo.pack(fill="x", pady=(0, 4))

        theme.make_separator(sidebar, theme.C_BORDER).pack(fill="x", pady=(12, 8))

        # ── Saved flakes list ─────────────────────────────────────────────────
        tk.Label(sidebar, text="Saved This Session",
                 font=theme.FONT_TITLE, fg=theme.C_TEXT, bg=theme.C_BG).pack(anchor="w")

        list_frame = tk.Frame(sidebar, bg=theme.C_SURFACE2,
                              highlightbackground=theme.C_BORDER, highlightthickness=1)
        list_frame.pack(fill="both", expand=True, pady=(6, 0))

        self._flake_listbox = tk.Listbox(
            list_frame,
            bg=theme.C_SURFACE2, fg=theme.C_TEXT,
            selectbackground=theme.C_ACCENT, selectforeground="white",
            font=theme.FONT_MONO, borderwidth=0, highlightthickness=0,
            activestyle="none",
        )
        sb = ttk.Scrollbar(list_frame, orient="vertical",
                           command=self._flake_listbox.yview)
        self._flake_listbox.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._flake_listbox.pack(fill="both", expand=True, padx=4, pady=4)

        # Point counter label
        self._pt_count_var = tk.StringVar(value="Points: 0")
        tk.Label(sidebar, textvariable=self._pt_count_var,
                 font=theme.FONT_SMALL, fg=theme.C_MUTED, bg=theme.C_BG).pack(
            anchor="w", pady=(6, 0))

    # ── Image loading ─────────────────────────────────────────────────────────

    def _load_image(self) -> None:
        try:
            self._raw_image = Image.open(self.image_info["full_path"])
        except Exception as exc:
            messagebox.showerror("Image Error", f"Cannot open image:\n{exc}", parent=self)
            self.destroy()
            return
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.after(50, self._fit_image_to_canvas)

    def _fit_image_to_canvas(self) -> None:
        if self._raw_image is None:
            return
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw < 10 or ch < 10:
            self.after(50, self._fit_image_to_canvas)
            return
        iw, ih = self._raw_image.size
        self._scale = min(cw / iw, ch / ih, 1.0)
        dw, dh = int(iw * self._scale), int(ih * self._scale)
        display = self._raw_image.resize((dw, dh), Image.LANCZOS)
        self._tk_image = ImageTk.PhotoImage(display)
        self.canvas.delete("all")
        self._poly_items.clear()
        # Centre image on canvas
        self._img_offset = ((cw - dw) // 2, (ch - dh) // 2)
        self.canvas.create_image(
            self._img_offset[0], self._img_offset[1],
            anchor="nw", image=self._tk_image, tags="bg_image",
        )
        # Re-draw any already-saved polygons this session
        for fl in self._session_flakes:
            self._redraw_saved_poly(fl["_canvas_pts"])

    def _on_canvas_resize(self, event) -> None:
        self.after(80, self._fit_image_to_canvas)

    # ── Canvas → image coordinate conversion ─────────────────────────────────

    def _canvas_to_image(self, cx: float, cy: float) -> tuple[int, int]:
        ox, oy = self._img_offset
        return int((cx - ox) / self._scale), int((cy - oy) / self._scale)

    # ── Polygon drawing events ────────────────────────────────────────────────

    def _on_click(self, event) -> None:
        self._drawing = True
        x, y = event.x, event.y
        self._points.append((x, y))
        # Draw vertex
        r = self.POINT_R
        self._poly_items.append(
            self.canvas.create_oval(x-r, y-r, x+r, y+r,
                                    fill=self.VERT_COL, outline="", tags="drawing")
        )
        # Draw edge to previous point
        if len(self._points) > 1:
            px, py = self._points[-2]
            self._poly_items.append(
                self.canvas.create_line(px, py, x, y,
                                        fill=self.POLY_COL, width=self.LINE_W, tags="drawing")
            )
        self._pt_count_var.set(f"Points: {len(self._points)}")

    def _on_motion(self, event) -> None:
        if not self._drawing or not self._points:
            return
        px, py = self._points[-1]
        if self._rubber_line:
            self.canvas.delete(self._rubber_line)
        self._rubber_line = self.canvas.create_line(
            px, py, event.x, event.y,
            fill=self.POLY_COL, width=self.LINE_W, dash=(4, 3), tags="rubber",
        )

    def _on_double_click(self, event) -> None:
        # The single-click already added the point; finish immediately
        self._finish_polygon()

    def _on_right_click(self, event) -> None:
        self._cancel_polygon()

    def _undo_point(self) -> None:
        if not self._points:
            return
        self._points.pop()
        # Remove vertex oval + preceding edge line (2 items per point after first)
        if self._poly_items:
            self.canvas.delete(self._poly_items.pop())   # oval
        if len(self._points) > 0 and self._poly_items:
            self.canvas.delete(self._poly_items.pop())   # edge
        self._pt_count_var.set(f"Points: {len(self._points)}")

    def _cancel_polygon(self) -> None:
        for item in self._poly_items:
            self.canvas.delete(item)
        if self._rubber_line:
            self.canvas.delete(self._rubber_line)
            self._rubber_line = None
        self._poly_items.clear()
        self._points.clear()
        self._drawing = False
        self._pt_count_var.set("Points: 0")
        self._hint_var.set("Left-click to add points  |  Double-click or Enter to finish  |  Right-click or Esc to cancel")

    def _finish_polygon(self) -> None:
        if len(self._points) < 3:
            messagebox.showwarning("Too few points",
                                   "Draw at least 3 points to define a flake contour.",
                                   parent=self)
            return
        # Close the polygon visually
        if self._rubber_line:
            self.canvas.delete(self._rubber_line)
            self._rubber_line = None
        x0, y0 = self._points[0]
        xn, yn = self._points[-1]
        close_line = self.canvas.create_line(
            xn, yn, x0, y0, fill=self.DONE_COL, width=self.LINE_W, tags="drawing"
        )
        self._poly_items.append(close_line)

        # Snapshot canvas coords before clearing state
        canvas_pts = list(self._points)

        # Convert to image coords
        img_pts = [self._canvas_to_image(cx, cy) for cx, cy in canvas_pts]

        # Compute geometry in image pixels
        max_side_px, min_side_px = _polygon_sidelengths(img_pts)
        cx_img, cy_img = _polygon_center(img_pts)

        max_side_um = px_to_um(max_side_px, self.zoom_str)
        min_side_um = px_to_um(min_side_px, self.zoom_str)

        self._drawing = False
        self._points.clear()
        self._poly_items.clear()
        self._pt_count_var.set("Points: 0")
        self._hint_var.set("Polygon saved ✓  —  Draw another or close the window")

        # Re-colour finished poly items green
        for item in self.canvas.find_withtag("drawing"):
            try:
                self.canvas.itemconfig(item, fill=self.DONE_COL, outline=self.DONE_COL)
            except Exception:
                pass
        self.canvas.dtag("drawing", "drawing")

        # Build and save record
        self._save_flake(img_pts, canvas_pts, cx_img, cy_img,
                         max_side_um, min_side_um)

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save_flake(
        self,
        img_pts:      list[tuple[int, int]],
        canvas_pts:   list[tuple[float, float]],
        cx:           int,
        cy:           int,
        max_side_um:  float,
        min_side_um:  float,
    ) -> None:
        try:
            thickness = float(self.v_thickness.get())
        except ValueError:
            thickness = 0.0

        row = self.parsed.get("row", 0)
        col = self.parsed.get("col", 0)
        chip = self.parsed.get("chip", "0")
        rc_tag = f"({row}, {col})"

        existing = _load_json(self.json_path)
        idx = _next_flake_index(existing, row, col)
        flake_id = f"{chip}_{rc_tag}_{idx:03d}"

        record = {
            "flake_id":           flake_id,
            "material":           self.v_material.get().strip(),
            "substrate":          self.v_substrate.get().strip(),
            "thickness_nm":       thickness,
            "layers":             self.v_layers.get().strip(),
            "center_xy":          [cx, cy],
            "(row, col)":         rc_tag,
            "max_sidelength_um":  max_side_um,
            "min_sidelength_um":  min_side_um,
            "date_exfoliated":    self.parsed.get("date_exfoliated", ""),
            "date_scanned":       self.v_date_scan.get().strip(),
            "user":               self.v_user.get().strip(),
        }

        existing.append(record)
        _save_json(self.json_path, existing)
        # Save .npy contour  (shape: N×2, integer image coords)
        npy_name = f"{flake_id}_contour.npy"
        npy_path = os.path.join(self.img_dir, npy_name)
        np.save(npy_path, np.array(img_pts, dtype=np.int32))

        # Track for sidebar
        record["_canvas_pts"] = canvas_pts
        self._session_flakes.append(record)
        self._flake_listbox.insert("end", flake_id)

        messagebox.showinfo(
            "Flake Saved",
            f"Flake '{flake_id}' saved.\n\n"
            f"JSON  → {os.path.basename(self.json_path)}\n"
            f"Contour → {npy_name}\n\n"
            f"Max side: {max_side_um:.3f} µm\n"
            f"Min side: {min_side_um:.3f} µm\n"
            f"Center: ({cx}, {cy})",
            parent=self,
        )

    def _redraw_saved_poly(self, canvas_pts: list[tuple[float, float]]) -> None:
        """Re-draw a previously saved polygon after canvas resize."""
        if len(canvas_pts) < 2:
            return
        for i, (x, y) in enumerate(canvas_pts):
            r = self.POINT_R
            self.canvas.create_oval(x-r, y-r, x+r, y+r,
                                    fill=self.DONE_COL, outline="")
            if i > 0:
                px, py = canvas_pts[i-1]
                self.canvas.create_line(px, py, x, y,
                                        fill=self.DONE_COL, width=self.LINE_W)
        # Close
        x0, y0 = canvas_pts[0]
        xn, yn = canvas_pts[-1]
        self.canvas.create_line(xn, yn, x0, y0,
                                fill=self.DONE_COL, width=self.LINE_W)


# ──────────────────────────────────────────────────────────────────────────────
#  Utility
# ──────────────────────────────────────────────────────────────────────────────
def _today_iso() -> str:
    from datetime import date
    return date.today().isoformat()


# ──────────────────────────────────────────────────────────────────────────────
#  Integration shim — drop this into action_add_flakes() in image_browser.py
# ──────────────────────────────────────────────────────────────────────────────
def open_annotator(parent, image_info: dict, date_scanned: str = "") -> None:
    """
    Call this from ImageBrowserApp.action_add_flakes():

        from flake_annotator import open_annotator

        def action_add_flakes(self) -> None:
            img_info = self.image_list[self.current_view_index]
            open_annotator(self, img_info, date_scanned=<scan_date_string>)
    """
    FlakeAnnotator(parent, image_info, date_scanned)


# ──────────────────────────────────────────────────────────────────────────────
#  Standalone test entry point
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()

    # Minimal stub — point this at a real image to test
    test_info = {
        "filename":  "060526_4_(2, 8)_5x.png",
        "full_path": os.path.join(os.path.dirname(__file__), "test_image.png"),
        "is_empty":  False,
    }

    if not os.path.exists(test_info["full_path"]):
        # Create a dummy 512×512 image for testing if none present
        img = Image.new("RGB", (512, 512), color=(30, 30, 60))
        img.save(test_info["full_path"])

    ann = FlakeAnnotator(root, test_info)
    ann.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()