"""
Core detection wrapper.

Wraps the vendored 2DMatGMM `MaterialDetector` with:
  - A (material, substrate)-aware loader that picks the right weights.
  - Caching so we don't rebuild the detector on every image.
  - A clean result dataclass that includes nm-thickness and confidence,
    so the GUI never has to touch the vendor module directly.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np

# Make the vendored package importable. We do this here, once, so the rest
# of flakefinder/ stays clean.
_VENDOR = os.path.join(os.path.dirname(__file__), "vendor")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

from src.autoscan.ffm.flakefinder.vendor.GMMDetector import MaterialDetector  # noqa: E402  (vendored)

from src.constants.configs import materials as M  # noqa: E402


class WeightsNotFoundError(FileNotFoundError):
    """Raised when no trained weights exist for the requested (material, substrate)."""

    def __init__(self, material: str, substrate: str, expected_path: str):
        super().__init__(
            f"No weights found for {material} on {substrate}. "
            f"Train them and place the file at: {expected_path}"
        )
        self.material = material
        self.substrate = substrate
        self.expected_path = expected_path


@dataclass
class FlakeResult:
    """One detected flake, in app-friendly form."""

    layers: str                    # raw label from model, e.g. "1", "2", "3"
    thickness_nm: Optional[float]  # None if layers isn't an integer count
    confidence: float              # 1 - false_positive_probability, in [0, 1]
    size_px: int
    center_xy: Tuple[int, int]
    max_sidelength_px: int
    min_sidelength_px: int
    mask: np.ndarray = field(repr=False)  # binary mask, same HxW as image

    def to_json_dict(self) -> dict:
        """Serialisable form (no numpy mask) for sidecar export."""
        return {
            "layers": self.layers,
            "thickness_nm": self.thickness_nm,
            "confidence": round(self.confidence, 4),
            "size_px": self.size_px,
            "center_xy": list(self.center_xy),
            "max_sidelength_px": self.max_sidelength_px,
            "min_sidelength_px": self.min_sidelength_px,
        }


@dataclass
class DetectionRun:
    """Everything one detection pass produces."""

    material: str
    substrate: str
    weights_path: str
    image_hw: Tuple[int, int]
    flakes: List[FlakeResult]
    elapsed_ms: float


class Detector:
    """
    Lazy, cached wrapper around `MaterialDetector`.

    Build one of these once and call `.detect(image, material, substrate)`
    repeatedly. Switching (material, substrate) transparently swaps the
    underlying model.
    """

    def __init__(
        self,
        size_threshold_px: int = 500,
        standard_deviation_threshold: float = 2.0,
        min_confidence: float = 0.0,
    ):
        self.size_threshold_px = size_threshold_px
        self.standard_deviation_threshold = standard_deviation_threshold
        self.min_confidence = min_confidence
        # Cache: (material, substrate) -> (MaterialDetector, weights_path)
        self._cache: dict = {}

    def _get_model(self, material: str, substrate: str):
        key = (material, substrate)
        if key in self._cache:
            return self._cache[key]

        weights_path = M.find_weights(material, substrate)
        if weights_path is None:
            raise WeightsNotFoundError(
                material, substrate, M.expected_weights_path(material, substrate)
            )

        contrast = M.load_contrast_dict(weights_path)
        model = MaterialDetector(
            contrast_dict=contrast,
            size_threshold=self.size_threshold_px,
            standard_deviation_threshold=self.standard_deviation_threshold,
            used_channels="BGR",
        )
        self._cache[key] = (model, weights_path)
        return self._cache[key]

    def detect(
        self,
        image_bgr: np.ndarray,
        material: str,
        substrate: str,
    ) -> DetectionRun:
        """
        Run detection on a BGR image (OpenCV convention).

        Returns a DetectionRun with all flakes above `min_confidence`,
        sorted by confidence (highest first) for nicer overlay drawing.
        """
        if material not in M.MATERIALS:
            raise ValueError(f"Unknown material: {material}")
        if substrate not in M.SUBSTRATES:
            raise ValueError(f"Unknown substrate: {substrate}")
        if image_bgr is None or image_bgr.ndim != 3 or image_bgr.shape[2] != 3:
            raise ValueError("Expected a 3-channel BGR image (HxWx3).")

        model, weights_path = self._get_model(material, substrate)

        t0 = time.perf_counter()
        raw_flakes = model(image_bgr)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        out: List[FlakeResult] = []
        for f in raw_flakes:
            if str(f.thickness) == "Background":
                # An explicit "this is just bare substrate" component used only
                # to keep background pixels from being claimed by a flake-layer
                # component whose Gaussian happens to sit close to contrast
                # (0, 0, 0). Never a real detection result.
                continue
            conf = float(1.0 - f.false_positive_probability)
            if conf < self.min_confidence:
                continue
            out.append(
                FlakeResult(
                    layers=str(f.thickness),
                    thickness_nm=M.layers_to_nm(material, str(f.thickness)),
                    confidence=conf,
                    size_px=int(f.size),
                    center_xy=(int(f.center[0]), int(f.center[1])),
                    max_sidelength_px=int(f.max_sidelength),
                    min_sidelength_px=int(f.min_sidelength),
                    mask=f.mask,
                )
            )

        out.sort(key=lambda x: x.confidence, reverse=True)

        return DetectionRun(
            material=material,
            substrate=substrate,
            weights_path=weights_path,
            image_hw=(image_bgr.shape[0], image_bgr.shape[1]),
            flakes=out,
            elapsed_ms=elapsed_ms,
        )
