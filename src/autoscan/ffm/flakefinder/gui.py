"""
FlakeFinder Tkinter GUI.

Layout (single window):
    +-----------------------------------------------------------+
    |  Controls (top)                                           |
    |   - Material dropdown                                     |
    |   - Substrate dropdown                                    |
    |   - Min confidence slider                                 |
    |   - Open image / Save overlay / Export JSON buttons       |
    +-----------------------------------------------------------+
    |                                                           |
    |  Image canvas (centre)                                    |
    |  Drop a microscope image here, or click "Open image"      |
    |                                                           |
    +-----------------------------------------------------------+
    |  Status bar (bottom)                                      |
    +-----------------------------------------------------------+

Drag-drop uses tkinterdnd2 if available; otherwise the open-image button
is the only way in. Detection runs on a worker thread so the UI stays
responsive on large images.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageTk

from src.autoscan.ffm.flakefinder.core import Detector, DetectionRun, WeightsNotFoundError
from src.constants.configs.materials import MATERIALS, SUBSTRATES
from .overlay import render_overlay


# Optional drag-drop. We degrade gracefully if tkinterdnd2 isn't installed.
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
    _DND_AVAILABLE = True
except ImportError:
    _DND_AVAILABLE = False


# Image extensions we'll attempt to open.
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")


class FlakeFinderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("FlakeFinder — 2D material thickness detection")
        self.root.geometry("1280x820")
        self.root.minsize(900, 600)

        # Detector is shared across detections (caches loaded weights).
        self.detector = Detector(
            size_threshold_px=500,
            standard_deviation_threshold=2.0,
            min_confidence=0.0,
        )

        # Most-recent state — kept so we can re-render when the user
        # changes the confidence slider without re-running detection.
        self.current_image_path: Optional[str] = None
        self.current_image_bgr: Optional[np.ndarray] = None
        self.current_run: Optional[DetectionRun] = None
        self.current_overlay_bgr: Optional[np.ndarray] = None
        self._tk_image_ref = None  # keep a reference so Tk doesn't gc the PhotoImage

        # Worker-thread <-> UI communication.
        # Each detection bumps `_run_gen`; results from older generations
        # are discarded so a slow worker can't overwrite a newer run.
        self._result_queue: "queue.Queue[tuple]" = queue.Queue()
        self._run_gen: int = 0

        self._build_ui()
        self._poll_result_queue()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        # Top control bar.
        controls = ttk.Frame(self.root, padding=(10, 8))
        controls.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(controls, text="Material:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.material_var = tk.StringVar(value="Graphene")
        self.material_combo = ttk.Combobox(
            controls,
            textvariable=self.material_var,
            values=[m.label for m in MATERIALS.values()],
            state="readonly",
            width=30,
        )
        self.material_combo.grid(row=0, column=1, padx=(0, 12))
        self.material_combo.bind("<<ComboboxSelected>>", lambda _: self._on_param_change())

        ttk.Label(controls, text="Substrate:").grid(row=0, column=2, sticky="w", padx=(0, 4))
        self.substrate_var = tk.StringVar(value="Si / 285 nm SiO\u2082")
        self.substrate_combo = ttk.Combobox(
            controls,
            textvariable=self.substrate_var,
            values=[s.label for s in SUBSTRATES.values()],
            state="readonly",
            width=22,
        )
        self.substrate_combo.grid(row=0, column=3, padx=(0, 12))
        self.substrate_combo.bind("<<ComboboxSelected>>", lambda _: self._on_param_change())

        ttk.Label(controls, text="Min confidence:").grid(row=0, column=4, sticky="w", padx=(8, 4))
        self.min_conf_var = tk.DoubleVar(value=0.3)
        self.min_conf_scale = ttk.Scale(
            controls,
            from_=0.0,
            to=1.0,
            orient=tk.HORIZONTAL,
            variable=self.min_conf_var,
            length=160,
            command=lambda _: self._on_min_conf_change(),
        )
        self.min_conf_scale.grid(row=0, column=5, padx=(0, 6))
        self.min_conf_label = ttk.Label(controls, text="30%", width=4)
        self.min_conf_label.grid(row=0, column=6, padx=(0, 12))

        # Action buttons.
        actions = ttk.Frame(self.root, padding=(10, 0))
        actions.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(actions, text="Open image…", command=self._open_image_dialog).pack(side=tk.LEFT)
        self.save_btn = ttk.Button(actions, text="Save overlay…", command=self._save_overlay, state="disabled")
        self.save_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.export_btn = ttk.Button(actions, text="Export JSON…", command=self._export_json, state="disabled")
        self.export_btn.pack(side=tk.LEFT, padx=(6, 0))

        # Canvas for image display.
        self.canvas = tk.Canvas(self.root, bg="#1c1c1f", highlightthickness=0)
        self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.canvas.bind("<Configure>", lambda _: self._redraw_canvas())

        # Drag-drop wiring.
        if _DND_AVAILABLE:
            self.canvas.drop_target_register(DND_FILES)  # type: ignore[attr-defined]
            self.canvas.dnd_bind("<<Drop>>", self._on_drop)  # type: ignore[attr-defined]
            placeholder = "Drop a microscope image here, or click \u201cOpen image\u2026\u201d"
        else:
            placeholder = "Click \u201cOpen image\u2026\u201d to load a microscope image"
        self._placeholder_text = placeholder

        # Status bar.
        self.status_var = tk.StringVar(value="Ready.")
        status = ttk.Label(
            self.root,
            textvariable=self.status_var,
            anchor="w",
            padding=(10, 4),
            relief="sunken",
        )
        status.pack(side=tk.BOTTOM, fill=tk.X)

        self._redraw_canvas()  # draw placeholder

    # ------------------------------------------------------------------
    # Parameter helpers
    # ------------------------------------------------------------------
    def _selected_material_key(self) -> str:
        label = self.material_var.get()
        for k, v in MATERIALS.items():
            if v.label == label:
                return k
        return "Graphene"

    def _selected_substrate_key(self) -> str:
        label = self.substrate_var.get()
        for k, v in SUBSTRATES.items():
            if v.label == label:
                return k
        return "SiO2_90nm"

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def _on_param_change(self) -> None:
        """Material or substrate changed — re-run detection on the current image."""
        if self.current_image_bgr is not None:
            self._run_detection_async()

    def _on_min_conf_change(self) -> None:
        """Just filter the existing detection; don't re-run."""
        v = self.min_conf_var.get()
        self.min_conf_label.configure(text=f"{int(round(v * 100))}%")
        if self.current_run is None:
            return
        self._render_and_show(self.current_run)

    def _open_image_dialog(self) -> None:
        path = filedialog.askopenfilename(
            title="Open microscope image",
            filetypes=[
                ("Image files", " ".join(f"*{e}" for e in _IMAGE_EXTS)),
                ("All files", "*.*"),
            ],
        )
        if path:
            self._load_image(path)

    def _on_drop(self, event) -> None:
        # tkinterdnd2 returns a space-separated string of paths, each possibly
        # wrapped in {} when it contains spaces. Take the first one.
        data = event.data.strip()
        if data.startswith("{") and "}" in data:
            path = data[1: data.index("}")]
        else:
            path = data.split()[0]
        if not os.path.isfile(path):
            self._set_status(f"Not a file: {path}")
            return
        if not path.lower().endswith(_IMAGE_EXTS):
            self._set_status(f"Unsupported file type: {os.path.basename(path)}")
            return
        self._load_image(path)

    def _save_overlay(self) -> None:
        if self.current_overlay_bgr is None:
            return
        default_name = "overlay.png"
        if self.current_image_path:
            stem = os.path.splitext(os.path.basename(self.current_image_path))[0]
            default_name = f"{stem}_overlay.png"
        path = filedialog.asksaveasfilename(
            title="Save overlay",
            defaultextension=".png",
            initialfile=default_name,
            filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg"), ("TIFF", "*.tif")],
        )
        if not path:
            return
        ok = cv2.imwrite(path, self.current_overlay_bgr)
        if ok:
            self._set_status(f"Saved overlay → {path}")
        else:
            messagebox.showerror("Save failed", f"Could not write to {path}")

    def _export_json(self) -> None:
        if self.current_run is None:
            return
        default_name = "flakes.json"
        if self.current_image_path:
            stem = os.path.splitext(os.path.basename(self.current_image_path))[0]
            default_name = f"{stem}_flakes.json"
        path = filedialog.asksaveasfilename(
            title="Export flake metadata",
            defaultextension=".json",
            initialfile=default_name,
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        threshold = self.min_conf_var.get()
        payload = {
            "image_path": self.current_image_path,
            "material": self.current_run.material,
            "substrate": self.current_run.substrate,
            "weights_path": self.current_run.weights_path,
            "image_height": self.current_run.image_hw[0],
            "image_width": self.current_run.image_hw[1],
            "elapsed_ms": round(self.current_run.elapsed_ms, 1),
            "min_confidence_filter": threshold,
            "flakes": [
                f.to_json_dict()
                for f in self.current_run.flakes
                if f.confidence >= threshold
            ],
        }
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
        self._set_status(f"Exported {len(payload['flakes'])} flake(s) → {path}")

    # ------------------------------------------------------------------
    # Detection pipeline
    # ------------------------------------------------------------------
    def _load_image(self, path: str) -> None:
        img = cv2.imread(path)
        if img is None:
            messagebox.showerror("Open failed", f"Could not read image:\n{path}")
            return
        self.current_image_path = path
        self.current_image_bgr = img
        self.current_run = None
        self.current_overlay_bgr = None
        self.save_btn.configure(state="disabled")
        self.export_btn.configure(state="disabled")
        self._set_status(f"Loaded {os.path.basename(path)}  ({img.shape[1]}\u00d7{img.shape[0]}). Detecting…")
        self._redraw_canvas()  # show raw image immediately
        self._run_detection_async()

    def _run_detection_async(self) -> None:
        if self.current_image_bgr is None:
            return
        material = self._selected_material_key()
        substrate = self._selected_substrate_key()
        image = self.current_image_bgr  # numpy arrays are safe to share read-only
        self._run_gen += 1
        gen = self._run_gen

        def worker():
            try:
                run = self.detector.detect(image, material, substrate)
                self._result_queue.put((gen, "ok", run))
            except WeightsNotFoundError as e:
                self._result_queue.put((gen, "no_weights", e))
            except Exception as e:  # noqa: BLE001 — surface anything else
                self._result_queue.put((gen, "error", e))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_result_queue(self) -> None:
        try:
            while True:
                gen, kind, payload = self._result_queue.get_nowait()
                if gen != self._run_gen:
                    # User changed material/substrate or loaded a new image
                    # before this worker finished. Drop the stale result.
                    continue
                if kind == "ok":
                    self.current_run = payload
                    self._render_and_show(payload)
                    self.save_btn.configure(state="normal")
                    self.export_btn.configure(state="normal")
                    above = sum(
                        1 for f in payload.flakes if f.confidence >= self.min_conf_var.get()
                    )
                    self._set_status(
                        f"{payload.material} on {payload.substrate}: "
                        f"{len(payload.flakes)} flake(s) detected "
                        f"({above} above threshold)  |  {payload.elapsed_ms:.0f} ms"
                    )
                elif kind == "no_weights":
                    e: WeightsNotFoundError = payload
                    self.current_run = None
                    self.current_overlay_bgr = None
                    self.save_btn.configure(state="disabled")
                    self.export_btn.configure(state="disabled")
                    self._redraw_canvas()
                    self._set_status(
                        f"No weights for {e.material} on {e.substrate}. "
                        f"Train them with train_substrate.py — see README."
                    )
                    messagebox.showwarning(
                        "Weights missing",
                        (
                            f"No trained weights for {e.material} on {e.substrate}.\n\n"
                            f"Expected file:\n{e.expected_path}\n\n"
                            "To create it, run:\n"
                            f"  python -m flakefinder.train_substrate \\\n"
                            f"      --material {e.material} --substrate {e.substrate} \\\n"
                            "      --images <folder of labeled images>\n\n"
                            "See README for the labelling layout."
                        ),
                    )
                elif kind == "error":
                    messagebox.showerror("Detection failed", str(payload))
                    self._set_status(f"Error: {payload}")
        except queue.Empty:
            pass
        # Re-arm.
        self.root.after(80, self._poll_result_queue)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _render_and_show(self, run: DetectionRun) -> None:
        threshold = self.min_conf_var.get()
        # Filter to flakes above threshold for the overlay we display + save.
        filtered = DetectionRun(
            material=run.material,
            substrate=run.substrate,
            weights_path=run.weights_path,
            image_hw=run.image_hw,
            flakes=[f for f in run.flakes if f.confidence >= threshold],
            elapsed_ms=run.elapsed_ms,
        )
        self.current_overlay_bgr = render_overlay(self.current_image_bgr, filtered)
        self._redraw_canvas()

    def _redraw_canvas(self) -> None:
        """Fit current image (overlay if available, else raw, else placeholder) into canvas."""
        self.canvas.delete("all")
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)

        # Pick what to display.
        if self.current_overlay_bgr is not None:
            disp = self.current_overlay_bgr
        elif self.current_image_bgr is not None:
            disp = self.current_image_bgr
        else:
            # Placeholder text only.
            self.canvas.create_text(
                cw // 2, ch // 2,
                text=self._placeholder_text,
                fill="#888",
                font=("TkDefaultFont", 14),
            )
            return

        # Fit-to-canvas while preserving aspect ratio.
        h, w = disp.shape[:2]
        scale = min(cw / w, ch / h)
        new_w = max(1, int(w * scale))
        new_h = max(1, int(h * scale))
        # OpenCV resize for speed; INTER_AREA when shrinking, INTER_LINEAR when growing.
        interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
        resized = cv2.resize(disp, (new_w, new_h), interpolation=interp)

        # BGR -> RGB for PIL.
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb)
        self._tk_image_ref = ImageTk.PhotoImage(pil_img)

        x = (cw - new_w) // 2
        y = (ch - new_h) // 2
        self.canvas.create_image(x, y, image=self._tk_image_ref, anchor="nw")

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)


def main() -> None:
    if _DND_AVAILABLE:
        root = TkinterDnD.Tk()  # type: ignore[attr-defined]
    else:
        root = tk.Tk()
    FlakeFinderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
