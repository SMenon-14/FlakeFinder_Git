"""
Annotated overlay rendering.

Given a BGR image and a DetectionRun, returns a BGR image with:
  - Per-flake coloured mask overlay (one colour per layer count).
  - A label per flake: "<layers>L  <nm> nm  <conf>%".
  - A small legend in the top-left.

Pure OpenCV/NumPy — no matplotlib dependency at runtime, so the GUI stays light.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import cv2
import numpy as np

from .core import DetectionRun, FlakeResult


# Per-layer colours (BGR). Higher layer counts -> warmer colours.
_LAYER_COLOURS_BGR: Dict[str, Tuple[int, int, int]] = {
    "1":  (255, 200,   0),   # cyan-ish blue
    "2":  (  0, 220,  80),   # green
    "3":  (  0, 180, 255),   # orange
    "4":  (  0,  80, 255),   # red-orange
    "5":  ( 80,   0, 220),   # magenta
    "TR":   (180,  80, 220),   # purple — tape residue
    "Bulk": ( 30,  30, 200),   # dark red — bulk crystal
}
_FALLBACK_COLOUR_BGR = (180, 180, 180)

# Labels that are not layer counts — displayed without the trailing "L".
_NON_LAYER_LABELS = {"TR", "Bulk"}


def _colour_for(layers: str) -> Tuple[int, int, int]:
    return _LAYER_COLOURS_BGR.get(layers, _FALLBACK_COLOUR_BGR)


def _format_label(flake: FlakeResult) -> str:
    if flake.layers in _NON_LAYER_LABELS:
        return f"{flake.layers}  {flake.confidence * 100:.0f}%"
    pieces = [f"{flake.layers}L"]
    if flake.thickness_nm is not None:
        pieces.append(f"{flake.thickness_nm:.2f} nm")
    pieces.append(f"{flake.confidence * 100:.0f}%")
    return "  ".join(pieces)


def _put_label_with_background(
    img: np.ndarray,
    text: str,
    anchor_xy: Tuple[int, int],
    fg: Tuple[int, int, int],
    scale: float = 0.55,
    thickness: int = 1,
) -> None:
    """Draw text with a filled dark background pill for legibility on noisy microscope images."""
    font = cv2.FONT_HERSHEY_SIMPLEX
    (tw, th), baseline = cv2.getTextSize(text, font, scale, thickness)
    x, y = anchor_xy
    # Keep on-canvas.
    x = max(2, min(x, img.shape[1] - tw - 6))
    y = max(th + 4, min(y, img.shape[0] - 4))

    pad = 3
    cv2.rectangle(
        img,
        (x - pad, y - th - pad),
        (x + tw + pad, y + baseline + pad),
        (20, 20, 20),
        thickness=-1,
    )
    cv2.putText(img, text, (x, y), font, scale, fg, thickness, lineType=cv2.LINE_AA)


def render_overlay(
    image_bgr: np.ndarray,
    run: DetectionRun,
    mask_alpha: float = 0.45,
    draw_contours: bool = True,
    draw_labels: bool = True,
    draw_legend: bool = True,
) -> np.ndarray:
    """
    Build an annotated BGR image. Does not modify the input.
    """
    if image_bgr is None or image_bgr.size == 0:
        raise ValueError("Empty image passed to render_overlay.")

    H, W = image_bgr.shape[:2]
    # Scale stroke width and text size with image dimension so the overlay
    # is readable on both 1500-px and 4000-px microscope images. Calibrated
    # so that a 1920-px image gets ~2 px contours and 0.55 text scale.
    px = max(W, H)
    contour_thickness = max(2, int(round(px / 1000.0)))
    label_scale = max(0.55, px / 3000.0 * 1.1)
    label_thickness = max(1, int(round(px / 1500.0)))

    out = image_bgr.copy()
    overlay = out.copy()

    # 1) Coloured fills per flake mask.
    for flake in run.flakes:
        colour = _colour_for(flake.layers)
        # Boolean indexing is the cheapest way to recolour by mask.
        m = flake.mask.astype(bool)
        overlay[m] = colour

    # Blend overlay back onto out.
    cv2.addWeighted(overlay, mask_alpha, out, 1.0 - mask_alpha, 0.0, dst=out)

    # 2) Contours per flake (drawn opaque on the blended image).
    if draw_contours:
        for flake in run.flakes:
            colour = _colour_for(flake.layers)
            contours, _ = cv2.findContours(
                flake.mask.astype(np.uint8),
                cv2.RETR_EXTERNAL,
                cv2.CHAIN_APPROX_SIMPLE,
            )
            cv2.drawContours(out, contours, -1, colour, thickness=contour_thickness, lineType=cv2.LINE_AA)

    # 3) Per-flake labels.
    if draw_labels:
        for flake in run.flakes:
            colour = _colour_for(flake.layers)
            cx, cy = flake.center_xy
            # Place label slightly above the centre point.
            _put_label_with_background(
                out,
                _format_label(flake),
                anchor_xy=(cx - 30, cy - 8),
                fg=colour,
                scale=label_scale,
                thickness=label_thickness,
            )

    # 4) Legend, top-left.
    if draw_legend:
        _draw_legend(out, run, px=px)

    return out


def _legend_key_for(label: str) -> str:
    s = label.strip()
    return s if s in _NON_LAYER_LABELS else f"{s}L"


def _draw_legend(img: np.ndarray, run: DetectionRun, px: int = 1920) -> None:
    """Compact legend describing run parameters + colour key."""
    # Try to look up the friendly substrate label, fall back to raw key.
    try:
        from .materials import SUBSTRATES
        sub_label = SUBSTRATES[run.substrate].label if run.substrate in SUBSTRATES else run.substrate.replace("_", " ")
    except Exception:
        sub_label = run.substrate.replace("_", " ")

    # OpenCV's Hershey fonts can't render non-ASCII. Replace the subscript-2
    # in "SiO₂" with plain "2" so the legend doesn't show "???".
    def _ascii(s: str) -> str:
        return s.replace("\u2082", "2").encode("ascii", "replace").decode("ascii")

    sub_label = _ascii(sub_label)
    lines = [
        _ascii(f"{run.material} on {sub_label}"),
        _ascii(f"{len(run.flakes)} flake(s) detected  |  {run.elapsed_ms:.0f} ms"),
    ]
    # Colour key — deduplicated by (key, colour) pair so demo labels with
    # different text but the same colour still collapse cleanly.
    seen: List[Tuple[str, Tuple[int, int, int]]] = []
    for fl in run.flakes:
        key = _legend_key_for(fl.layers)
        col = _colour_for(fl.layers)
        if (key, col) not in seen:
            seen.append((key, col))

    font = cv2.FONT_HERSHEY_SIMPLEX
    # Make legend text scale more aggressively with image size so it's readable
    # even on 4K microscope captures.
    scale = max(0.55, px / 2500.0)
    th = max(1, int(round(px / 1500.0)))
    x0, y0 = 10, 10
    line_h = int(22 + scale * 18)
    pad_top = int(10 + scale * 18)

    # Measure each chip's label so the panel width fits whatever's present.
    chip_box = int(14 + scale * 10)
    chip_gap = int(8 + scale * 6)
    chip_widths = []
    for key, _ in seen:
        (tw, _), _ = cv2.getTextSize(key, font, scale, th)
        chip_widths.append(chip_box + chip_gap + tw + int(chip_gap * 1.5))

    chips_total_w = sum(chip_widths) if chip_widths else 0
    panel_w = max(int(320 + scale * 200), x0 + 8 + chips_total_w)
    panel_h = pad_top + line_h * len(lines) + (line_h if seen else 0)
    cv2.rectangle(img, (x0, y0), (x0 + panel_w, y0 + panel_h), (20, 20, 20), thickness=-1)

    y = y0 + pad_top
    for line in lines:
        cv2.putText(img, line, (x0 + 8, y), font, scale, (240, 240, 240), th, cv2.LINE_AA)
        y += line_h

    if seen:
        y += int(scale * 4)
        x = x0 + 8
        chip_h = int(chip_box * 0.9)
        for (key, colour), wid in zip(seen, chip_widths):
            cv2.rectangle(img, (x, y - chip_h), (x + chip_box, y + 2), colour, thickness=-1)
            cv2.putText(img, key, (x + chip_box + chip_gap, y), font, scale, (240, 240, 240), th, cv2.LINE_AA)
            x += wid
