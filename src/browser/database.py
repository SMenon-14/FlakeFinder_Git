import json
import threading
import numpy as np
from pathlib import Path
from PIL import Image, ImageDraw
from tkinter import messagebox

from src.browser.constants import CONTOUR_COLOR
from src.constants.configs.materials import MATERIALS

# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_int(v):
    try:    return int(v)
    except: return 0


# ── Data Layer ────────────────────────────────────────────────────────────────

class FlakeDatabase:
    """
    Walks  images/ScanningSession_*/chipnum/  directories, loads every JSON
    sidecar, and builds a flat list of flake dicts.

    New JSON schema keys used:
        flake_id, material, substrate, thickness_nm, layers,
        center_xy, (row, col), max_sidelength_um, min_sidelength_um,
        date_exfoliated, date_scanned

    Internal keys added to every entry:
        _json_path, _image_path, _chip_folder, _session, _chip_num
    """

    def __init__(self, folder: str, autoload: bool = True):
        self.folder    = Path(folder)
        self.flakes: list[dict] = []
        self.materials: list[str] = self._load_materials()
        if autoload:
            self.reload()

    # ── Scanning ──────────────────────────────────────────────────────────────

    def reload_async(self, on_done=None, root=None):
        """Scan the images folder on a background thread.

        Walking a large images/contours tree can take a long time the first
        time Windows/AV touches every file, so this keeps the UI thread (and
        window creation) unblocked. `on_done` is invoked once `self.flakes`
        has been replaced, marshalled onto `root` via `after` if given.
        """
        def worker():
            self.reload()
            if on_done:
                if root is not None:
                    root.after(0, on_done)
                else:
                    on_done()
        threading.Thread(target=worker, daemon=True).start()

    def reload(self):
        flakes: list[dict] = []
        if not self.folder.exists():
            self.flakes = flakes
            return

        for session_dir in sorted(self.folder.iterdir()):
            if not session_dir.is_dir():
                continue
            if not session_dir.name.startswith("ScanningSession_"):
                continue
            if "empty" in session_dir.name.lower():
                continue

            session_name = session_dir.name
            date_dirs    = [x for x in sorted(session_dir.iterdir()) if x.is_dir()]

            for date_dir in date_dirs:
                if "empty" in date_dir.name.lower():
                    continue

                chip_dirs = [x for x in sorted(date_dir.iterdir()) if x.is_dir()]

                for chip_dir in chip_dirs:
                    if "empty" in chip_dir.name.lower():
                        continue

                    chip_num   = chip_dir.name
                    json_files = sorted(chip_dir.glob("*.json"))

                    for json_path in json_files:
                        image_path = self._find_image(json_path)
                        try:
                            with open(json_path, "r") as f:
                                flake_list = json.load(f)
                            if not isinstance(flake_list, list):
                                continue
                            for flake in flake_list:
                                entry = dict(flake)
                                entry["_json_path"]   = str(json_path)
                                entry["_image_path"]  = str(image_path) if image_path else ""
                                entry["_chip_folder"] = str(chip_dir)
                                entry["_session"]     = session_name
                                entry["_chip_num"]    = chip_num
                                entry.setdefault("used", False)
                                flakes.append(entry)
                        except (json.JSONDecodeError, OSError) as e:
                            continue

        self.flakes = flakes

    def _find_image(self, json_path: Path) -> Path | None:
        for ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"):
            candidate = json_path.with_suffix(ext)
            if candidate.exists():
                return candidate
        return None

    # ── Contour Overlay ───────────────────────────────────────────────────────

    def contour_paths(self, flake: dict) -> list[Path]:
        chip_dir = Path(flake.get("_chip_folder", ""))
        flake_id = flake.get("flake_id", "")

        if not chip_dir.exists() or not flake_id:
            return []

        all_files = list(chip_dir.iterdir())

        npy_files = sorted(chip_dir.glob("*.npy"))

        candidate = chip_dir / f"{flake_id}.npy"
        if candidate.exists():
            return [candidate]

        glob_results = sorted(chip_dir.glob(f"{flake_id}*.npy"))
        return glob_results

    def build_outlined_image(self, flake: dict) -> Image.Image | None:
        image_path = flake.get("_image_path", "")
        if not image_path:
            return None
        try:
            base = Image.open(image_path).convert("RGBA")
        except Exception as e:
            return None

        overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        draw    = ImageDraw.Draw(overlay)

        paths = self.contour_paths(flake)

        for npy_path in paths:
            try:
                contour = np.load(str(npy_path))
                pts = contour.reshape(-1, 2)
                xy  = [(int(p[0]), int(p[1])) for p in pts]
                xs = [p[0] for p in xy]
                ys = [p[1] for p in xy]
                if len(xy) >= 2:
                    draw.line(xy + [xy[0]], fill=CONTOUR_COLOR, width=8)
            except Exception as e:
                continue

        composite = Image.alpha_composite(base, overlay)
        return composite.convert("RGB")

    # ── Used Flag ─────────────────────────────────────────────────────────────

    def set_used(self, flake: dict, used: bool):
        json_path = Path(flake["_json_path"])
        try:
            with open(json_path, "r") as f:
                data = json.load(f)
            for entry in data:
                if entry.get("flake_id") == flake.get("flake_id"):
                    entry["used"] = used
                    break
            with open(json_path, "w") as f:
                json.dump(data, f, indent=2)
            flake["used"] = used
        except (OSError, json.JSONDecodeError) as e:
            messagebox.showerror("Save Error", f"Could not save: {e}")

    # ── Materials List ────────────────────────────────────────────────────────

    def _load_materials(self) -> list[str]:
        return list(MATERIALS)

    # ── Search / Filter ───────────────────────────────────────────────────────

    def search(self,
               text: str = "",
               material: str = "",
               size_min: float = None,
               size_max: float = None,
               layers_min: int = None,
               layers_max: int = None,
               date_exfoliated: str = "",
               date_scanned: str = "",
               usage_status: str = "",
               user: str = "",
               chip: str = "",
               sort_field: str = "",
               sort_asc: bool = True) -> list[dict]:

        results    = []
        text_lower = text.lower()

        for flake in self.flakes:
            if text_lower:
                haystack = " ".join(str(v) for v in flake.values()).lower()
                if text_lower not in haystack:
                    continue

            if material.lower() not in str(flake.get("material", "")).lower():
                continue

            # size filter uses max_sidelength_um
            size = flake.get("max_sidelength_um")
            if size_min is not None and (size is None or size < size_min):
                continue
            if size_max is not None and (size is None or size > size_max):
                continue

            # layers filter
            try:
                layers = int(flake.get("layers", 0))
            except (ValueError, TypeError):
                layers = None
            if layers_min is not None and (layers is None or layers < layers_min):
                continue
            if layers_max is not None and (layers is None or layers > layers_max):
                continue

            if date_exfoliated and not str(flake.get("date_exfoliated", "")).startswith(date_exfoliated):
                continue
            if date_scanned and not str(flake.get("date_scanned", "")).startswith(date_scanned):
                continue

            if usage_status == "Available" and flake.get("used", False):
                continue
            if usage_status == "Used" and not flake.get("used", False):
                continue

            if user.lower() not in str(flake.get("user", "")).lower():
                continue

            if chip.lower() not in str(flake.get("_chip_num", "")).lower():
                continue

            results.append(flake)

        if sort_field:
            key_map = {
                "Material":        lambda f: str(f.get("material", "")),
                "Layers":          lambda f: _safe_int(f.get("layers", 0)),
                "Max Size (µm)":   lambda f: f.get("max_sidelength_um") or 0,
                "Date Exfoliated": lambda f: str(f.get("date_exfoliated", "")),
                "Date Scanned":    lambda f: str(f.get("date_scanned", "")),
                "Usage Status":    lambda f: str(f.get("used", False)),
                "Session":         lambda f: str(f.get("_session", "")),
                "Chip":            lambda f: str(f.get("_chip_num", "")),
                "User":            lambda f: str(f.get("user", "")),
            }
            if sort_field in key_map:
                results.sort(key=key_map[sort_field], reverse=not sort_asc)

        return results
