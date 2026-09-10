"""
Material and substrate registry for FlakeFinder.

Centralises:
  - Per-material physical properties (interlayer spacing for nm thickness).
  - Substrate identifiers.
  - The mapping from (material, substrate) -> trained-weight file.

The shipped weights from 2DMatGMM cover Graphene (and WSe2) on Si / 90 nm SiO2.
Anything else requires training; we expose the expected weight-file path
so the GUI can show a clear "weights missing" message.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional
from src.paths import MODEL_DIR

# Vendor directory (next to this file's parent) — used to locate shipped weights.
VENDOR_DIR = MODEL_DIR / 'flakefinder' / 'vendor'
SHIPPED_WEIGHTS_DIR = VENDOR_DIR / 'GMMDetector' / 'trained_parameters'

# User-trained weights go here so we never overwrite vendored files.
USER_WEIGHTS_DIR = MODEL_DIR / 'flakefinder' / 'user_weights'
os.makedirs(USER_WEIGHTS_DIR, exist_ok=True)


@dataclass(frozen=True)
class Material:
    name: str
    # Interlayer spacing in nm. Used to convert a discrete layer count
    # (the only thing the GMM model outputs) into an approximate physical
    # thickness. These are textbook c-axis values for crystalline forms.
    interlayer_nm: float
    # Pretty label for the GUI dropdown.
    label: str


MATERIALS = {
    "Graphene": Material(name="Graphene", interlayer_nm=0.335, label="Graphene"),
    "hBN":      Material(name="hBN",      interlayer_nm=0.333, label="hBN (hexagonal boron nitride)"),
}


@dataclass(frozen=True)
class Substrate:
    key: str
    label: str


# Order matters: GUI shows them in this order.
SUBSTRATES = {
    "SiO2_285nm": Substrate("SiO2_285nm", "Si / 285 nm SiO\u2082"),
    "SiO2_90nm":  Substrate("SiO2_90nm",  "Si / 90 nm SiO\u2082"),
}


def weights_filename(material: str, substrate: str) -> str:
    """Canonical filename for a (material, substrate) weight set."""
    return f"{material}_{substrate}_GMM.json"


def find_weights(material: str, substrate: str) -> Optional[str]:
    """
    Locate weights for (material, substrate).

    Search order:
      1. user_weights/{Material}_{Substrate}_GMM.json   (locally trained)
      2. vendor shipped weights, but ONLY when substrate is the 90 nm SiO2
         that 2DMatGMM was trained on. Using shipped weights on a different
         oxide would silently mis-classify layers, so we refuse rather than
         risk it.

    Returns absolute path if found, else None.
    """
    # 1) Locally trained weights take priority (user knows their setup).
    user_path = os.path.join(USER_WEIGHTS_DIR, weights_filename(material, substrate))
    if os.path.exists(user_path):
        return user_path

    # 2) Shipped weights only valid for the substrate they were trained on.
    if substrate == "SiO2_90nm":
        shipped = os.path.join(SHIPPED_WEIGHTS_DIR, f"{material}_GMM.json")
        if os.path.exists(shipped):
            return shipped

    return None


def load_contrast_dict(path: str) -> dict:
    with open(path, "r") as f:
        return json.load(f)


def layers_to_nm(material: str, layer_label: str) -> Optional[float]:
    """
    Convert the model's layer label (e.g. "1", "2", "3") to a thickness in nm.

    Returns None if the label isn't an integer count — some custom training
    might use labels like 'bulk' or '10-40nm', which can't be converted this way.
    """
    try:
        n = int(layer_label)
    except (TypeError, ValueError):
        return None
    if material not in MATERIALS:
        return None
    return round(n * MATERIALS[material].interlayer_nm, 3)


def expected_weights_path(material: str, substrate: str) -> str:
    """Where a user *should* place trained weights for (material, substrate)."""
    return os.path.join(USER_WEIGHTS_DIR, weights_filename(material, substrate))
