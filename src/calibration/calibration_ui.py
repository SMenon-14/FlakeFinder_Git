import os
import sys
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog
from PIL import Image, ImageTk
from src.calibration.calibrator import CalibrationManager
from src.constants.themes import theme



if sys.platform.startswith("win"):
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1) # Value 1 = Process_System_DPI_Aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware() # Fallback for older Windows versions
        except Exception:
            pass


class CalibrationWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.update()
        self.update_idletasks()

        self.attributes("-fullscreen", True)
        self.bind("<Escape>", lambda e: self.attributes("-fullscreen", False))

        self.configure(bg=theme.C_BG)
        theme.setup_ttk_styles()

        self.manager              = CalibrationManager()
        self.start_coords         = None
        self.current_line         = None
        self.calculated_um_per_px = 0.0
        self.image_path           = None
        self.tk_image             = None
        self.display_scale        = 1.0

        self._setup_ui()

    # ── UI Construction ───────────────────────────────────────────────────────

    def _setup_ui(self):
        tk.Frame(self, bg=theme.C_ACCENT, height=3).pack(fill="x", side="top")

        self._build_combined_bar()

        theme.make_separator(self, theme.C_BORDER).pack(fill="x")

        self.canvas = tk.Canvas(self, bg="#1e1e2e", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<ButtonPress-1>",   self.on_click)
        self.canvas.bind("<B1-Motion>",       self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)

        self._hint_id = self.canvas.create_text(
            0, 0,
            text="Load an image to begin  ·  Draw a line over the scale bar",
            fill=theme.C_MUTED, font=theme.FONT_BODY, anchor="nw",
        )
        self.canvas.bind("<Configure>", self._reposition_hint)

    def _build_combined_bar(self):
        bar = tk.Frame(
            self, bg=theme.C_SURFACE,
            highlightbackground=theme.C_BORDER, highlightthickness=1,
            height=48,
        )
        bar.pack(fill="x")
        bar.pack_propagate(False)

        # ── Left: title + controls ────────────────────────────────────────────
        tk.Label(
            bar, text="Calibration Tool",
            font=("Segoe UI Semibold", 13),
            fg=theme.C_TEXT, bg=theme.C_SURFACE,
        ).pack(side="left", padx=(22, 18))

        theme.make_separator(bar, theme.C_BORDER, thickness=18).pack(
            side="left", pady=12
        )

        btn_load = ttk.Button(bar, text="Load Image", command=self.load_image)
        theme.apply_button_style(btn_load, primary=True)
        btn_load.pack(side="left", padx=(12, 14))

        self._add_labeled_entry(bar, "Zoom Level:", width=7,
                                attr="_entry_zoom", placeholder="e.g. 10")
        self._add_labeled_entry(bar, "Bar Size (µm):", width=7,
                                attr="_entry_scale", default="10")

        self._lbl_result = tk.Label(
            bar, text="µm/px:  —",
            font=("Segoe UI Semibold", 11),
            fg=theme.C_ACCENT, bg=theme.C_SURFACE,
        )
        self._lbl_result.pack(side="left", padx=18)

        # ── Right: save + nav labels ──────────────────────────────────────────
        def _nav_btn(text, fg_normal, command):
            lbl = tk.Label(
                bar, text=text, font=theme.FONT_SMALL,
                fg=fg_normal, bg=theme.C_SURFACE, cursor="hand2",
            )
            lbl.bind("<Enter>",    lambda e: lbl.config(fg=theme.C_TEXT))
            lbl.bind("<Leave>",    lambda e: lbl.config(fg=fg_normal))
            lbl.bind("<Button-1>", lambda e: command())
            return lbl

        _nav_btn("✕  Close", theme.C_USED,  self.destroy).pack(side="right", padx=(8, 16))

        btn_save = ttk.Button(bar, text="Save Entry", command=self.save_data)
        theme.apply_button_style(btn_save, primary=True)
        btn_save.pack(side="right", padx=(0, 8))

    def _add_labeled_entry(self, parent, label_text, width, attr,
                           placeholder=None, default=None):
        """Helper: pack a label + themed entry into parent, store entry as self.<attr>."""
        tk.Label(
            parent, text=label_text,
            font=theme.FONT_HEAD, fg=theme.C_MUTED, bg=theme.C_SURFACE,
        ).pack(side="left", padx=(0, 4))

        frame = tk.Frame(
            parent, bg=theme.C_SURFACE2,
            highlightbackground=theme.C_BORDER, highlightthickness=1,
        )
        frame.pack(side="left", padx=(0, 12))

        entry = tk.Entry(
            frame, width=width, font=theme.FONT_BODY,
            bg=theme.C_SURFACE2, fg=theme.C_TEXT,
            insertbackground=theme.C_TEXT, relief="flat",
        )
        entry.pack(padx=6, pady=5)

        if default is not None:
            entry.insert(0, default)

        # Border highlight on focus
        entry.bind("<FocusIn>",  lambda e: frame.config(highlightbackground=theme.C_ACCENT))
        entry.bind("<FocusOut>", lambda e: frame.config(highlightbackground=theme.C_BORDER))

        setattr(self, attr, entry)

    # ── Hint overlay ──────────────────────────────────────────────────────────

    def _reposition_hint(self, event=None):
        if self._hint_id:
            self.canvas.coords(self._hint_id, 20, 20)

    # ── Image Loading ─────────────────────────────────────────────────────────

    def load_image(self):
        path = filedialog.askopenfilename(
            filetypes=[("Image Files", "*.png *.jpg *.jpeg *.tif *.tiff")]
        )
        if not path:
            return

        self.image_path = path
        self.canvas.delete("all")
        self._hint_id = None

        self.update_idletasks()
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()

        img = Image.open(path)
        orig_w, orig_h = img.size

        # Scale to fit canvas while preserving aspect ratio
        scale = min(canvas_w / orig_w, canvas_h / orig_h)
        self.display_scale = scale  # store for coordinate correction in on_release

        new_w = int(orig_w * scale)
        new_h = int(orig_h * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)

        self.tk_image = ImageTk.PhotoImage(img)

        cx = canvas_w // 2
        cy = canvas_h // 2
        self.canvas.create_image(cx, cy, anchor="center", image=self.tk_image)
        self.canvas.config(scrollregion=self.canvas.bbox("all"))

    # ── Drawing Interaction ───────────────────────────────────────────────────

    def on_click(self, event):
        self.start_coords = (event.x, event.y)
        if self.current_line:
            self.canvas.delete(self.current_line)
            self.current_line = None

    def on_drag(self, event):
        if not self.start_coords:
            return
        if self.current_line:
            self.canvas.delete(self.current_line)
        x1, y1 = self.start_coords

        self.current_line = self.canvas.create_line(
            x1, y1, event.x, event.y,
            fill=theme.C_ACCENT, width=2,
        )

    def on_release(self, event):
        if not self.start_coords:
            return
        try:
            scale_value = float(self._entry_scale.get().strip())
        except ValueError:
            self._set_result("Error: invalid bar size", error=True)
            return

        # Correct for display scaling — convert screen pixels back to real image pixels
        x1, y1 = self.start_coords
        x2, y2 = event.x, event.y

        real_start = (x1 / self.display_scale, y1 / self.display_scale)
        real_end   = (x2 / self.display_scale, y2 / self.display_scale)

        self.calculated_um_per_px = self.manager.calculate_um_per_pixel(
            real_start, real_end, scale_value
        )
        self._set_result(f"µm/px:  {self.calculated_um_per_px:.6f}")

    # ── Save ──────────────────────────────────────────────────────────────────

    def save_data(self):
        zoom = self._entry_zoom.get().strip()
        if not zoom:
            self._set_result("Error: missing zoom value", error=True)
            return
        if self.calculated_um_per_px == 0.0:
            self._set_result("Error: draw scale line first", error=True)
            return

        self.manager.save_calibration(zoom, self.calculated_um_per_px)
        self._set_result(f"Saved  ·  Zoom {zoom}", ok=True)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_result(self, text, error=False, ok=False):
        color = theme.C_USED if error else (theme.C_AVAIL if ok else theme.C_ACCENT)
        self._lbl_result.config(text=text, fg=color)


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = CalibrationWindow()
    app.mainloop()