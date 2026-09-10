"""
Train GMM weights for a new (material, substrate) combination.

Quick-start labelling layout
----------------------------
Point this script at a folder organised like:

    <data_dir>/
        train_images/
            img001.png
            img002.png
            ...
        train_semantic_masks/
            img001.png   # 8-bit greyscale, same filename as image
            img002.png   # pixel value = layer count (1, 2, 3, ...);
                         # pixel value = 200 for tape residue;
                         # 0 = background, ignored
            ...

Use prepare_training_data.py to convert LabelMe JSON annotations into this
layout automatically.  Label flake regions as "1", "2", "3", etc. and tape
residue as "TR".  Five images per layer count is plenty.

For each layer count (1, 2, 3, ... up to --num-layers), tape residue, and
bulk crystal, the script fits one Gaussian directly to that label's own
pixels in BGR-contrast space, and writes everything to:

    flakefinder/user_weights/<Material>_<Substrate>_GMM.json

After training, the GUI will pick up the new weights automatically.

Example
-------
    python -m flakefinder.train_substrate \\
        --material Graphene --substrate SiO2_285nm \\
        --data-dir ./my_285nm_dataset --num-layers 4
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import cv2
import numpy as np

# Make the vendored GMMDetector importable.
_THIS = os.path.dirname(__file__)
_VENDOR = os.path.join(_THIS, "vendor")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

from . import materials as M  # noqa: E402
from src.autoscan.ffm.flakefinder.vendor.GMMDetector import MaterialDetector  # noqa: E402  (vendored)


# Mask values used by prepare_training_data.py for special regions.
_TR_MASK_VALUE   = 200
_BULK_MASK_VALUE = 201


def _calculate_background_color(img: np.ndarray, radius: int = 5) -> np.ndarray:
    """Mean BGR colour of the bare substrate (replicates the vendor helper)."""
    masks = []
    for i in range(3):
        ch = img[:, :, i]
        mask = cv2.inRange(ch, 20, 230)
        hist = cv2.calcHist([ch], [0], mask, [256], [0, 256])
        mode = int(np.argmax(hist))
        tmask = cv2.inRange(ch, max(0, mode - radius), min(255, mode + radius))
        tmask = cv2.erode(tmask, np.ones((3, 3)), iterations=3)
        masks.append(tmask)
    combined = cv2.bitwise_and(masks[0], masks[1])
    combined = cv2.bitwise_and(combined, masks[2])
    return np.array(cv2.mean(img, mask=combined)[:3], dtype=np.float32)


def _augment_image(image: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """
    Apply a random camera-variation augmentation to a BGR image.

    Simulates the two main sources of inter-camera/inter-session variation:
      - Exposure shift  : overall brightness up/down (±35 %)
      - White balance   : independent per-channel gain (±12 % each)

    The GMM normalises by background, so pure brightness changes nearly cancel
    out — but per-channel gain (colour temperature) does NOT cancel and is the
    primary source of cross-camera contrast drift.  We do NOT apply geometric
    augmentations (flip/rotate) because the GMM only cares about pixel values,
    not spatial layout.
    """
    aug = image.astype(np.float32)
    aug *= rng.uniform(0.65, 1.35)                          # exposure
    aug *= rng.uniform(0.88, 1.12, size=(1, 1, 3))          # per-channel (white balance)
    return np.clip(aug, 0, 255).astype(np.uint8)


def _collect_contrasts(
    image_dir: str,
    mask_dir: str,
    augment_copies: int = 4,
    seed: int = 0,
    max_pixels_per_region: int = 20000,
) -> tuple[dict[int, np.ndarray], np.ndarray, np.ndarray, np.ndarray]:
    """
    Read all image/mask pairs and return:
      layer_contrasts : {layer_count: contrast array} for each flake-layer
                         value (1..199) found in any mask
      tr_contrasts    : pixels where mask value is 200  (tape residue)
      bulk_contrasts  : pixels where mask value is 201  (bulk crystal)
      background_contrasts : unlabelled (mask value 0) substrate pixels

    Each image is used augment_copies+1 times (original + N augmented variants)
    to make each layer's Gaussian robust to camera-setting variation.

    Each individual labelled region (one flake-layer polygon's worth of pixels
    in one image, or one image's TR/Bulk/background pixels) contributes at most
    `max_pixels_per_region` pixels per version. Without this cap, one huge
    polygon (e.g. a 500k-pixel flake) can dominate that layer's fitted
    Gaussian and make smaller regions of the same layer in other images look
    like outliers.
    """
    layer_rows: dict[int, list[np.ndarray]] = {}
    tr_rows:    list[np.ndarray] = []
    bulk_rows:  list[np.ndarray] = []
    background_rows: list[np.ndarray] = []

    rng = np.random.default_rng(seed)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

    mask_names = sorted(os.listdir(mask_dir))
    for idx, mask_name in enumerate(mask_names):
        print(f"  {idx + 1}/{len(mask_names)} {mask_name}", end="\r")
        img_path = os.path.join(image_dir, mask_name)
        if not os.path.exists(img_path):
            img_path = os.path.join(image_dir, os.path.splitext(mask_name)[0] + ".jpg")
        if not os.path.exists(img_path):
            print(f"\n  WARNING: no image found for mask {mask_name}, skipping")
            continue

        mask = cv2.imread(os.path.join(mask_dir, mask_name), cv2.IMREAD_GRAYSCALE)
        image = cv2.imread(img_path)
        if mask is None or image is None:
            continue

        # One eroded binary mask per labelled region: each individual flake
        # layer value (1..199) gets its own mask (and its own row list), plus
        # TR (200) and Bulk (201). Keeping layers separate here is what lets
        # us cap each one independently below and fit each one its own
        # Gaussian later.
        region_masks: list[tuple[np.ndarray, list]] = []
        flake_values = sorted(int(v) for v in np.unique(mask) if 0 < v < _TR_MASK_VALUE)
        for value in flake_values:
            binary = np.where(mask == value, 255, 0).astype(np.uint8)
            eroded = cv2.erode(binary, kernel, iterations=1)
            region_masks.append((eroded, layer_rows.setdefault(value, [])))
        for value, rows in [(_TR_MASK_VALUE, tr_rows), (_BULK_MASK_VALUE, bulk_rows)]:
            binary = np.where(mask == value, 255, 0).astype(np.uint8)
            region_masks.append((cv2.erode(binary, kernel, iterations=1), rows))

        # Unlabelled (mask == 0) pixels are bare substrate. Collecting these
        # too lets us fit an explicit "Background" component, so pixels that
        # are genuinely just substrate compete on equal footing against the
        # flake-layer components instead of relying solely on each layer's
        # Gaussian staying >2 std devs away from contrast (0, 0, 0).
        background_binary = np.where(mask == 0, 255, 0).astype(np.uint8)
        region_masks.append((cv2.erode(background_binary, kernel, iterations=1), background_rows))

        versions = [image] + [_augment_image(image, rng) for _ in range(augment_copies)]
        for img_version in versions:
            bg = _calculate_background_color(img_version)
            if np.any(bg == 0):
                # Cross-channel masking can fail for noisy/textured/high-res images
                # (the per-channel near-mode masks no longer overlap after erosion).
                # Fall back to the vendor's per-channel-independent estimator so
                # these images still contribute training data.
                bg = MaterialDetector.get_mean_background_values_numba(img_version)
                if np.any(bg == 0):
                    continue
            img_f = img_version.astype(np.float32)
            for eroded, rows in region_masks:
                ys, xs = np.nonzero(eroded)
                if ys.size == 0:
                    continue
                if ys.size > max_pixels_per_region:
                    pick = rng.choice(ys.size, size=max_pixels_per_region, replace=False)
                    ys, xs = ys[pick], xs[pick]
                rows.append(img_f[ys, xs] / bg - 1.0)

    print()
    layer_contrasts = {v: np.vstack(rows) for v, rows in layer_rows.items() if rows}
    tr_arr   = np.vstack(tr_rows)   if tr_rows   else np.empty((0, 3))
    bulk_arr = np.vstack(bulk_rows) if bulk_rows else np.empty((0, 3))
    background_arr = np.vstack(background_rows) if background_rows else np.empty((0, 3))
    return layer_contrasts, tr_arr, bulk_arr, background_arr


def _fit_single_gaussian(contrasts: np.ndarray, label: str) -> dict | None:
    """Fit one Gaussian to a set of contrast pixels. Returns a weights-dict entry or None."""
    if contrasts.shape[0] < 30:
        print(f"  WARNING: only {contrasts.shape[0]} {label} pixels — skipping {label} component.")
        return None
    mean = contrasts.mean(axis=0)
    cov  = np.cov(contrasts, rowvar=False)
    print(f"  {label} component: mean BGR = [{mean[0]:.4f}, {mean[1]:.4f}, {mean[2]:.4f}]  "
          f"({contrasts.shape[0]:,} pixels)")
    return {
        "contrast": {"b": float(mean[0]), "g": float(mean[1]), "r": float(mean[2])},
        "covariance_matrix": cov.tolist(),
    }


def train(
    material: str,
    substrate: str,
    data_dir: str,
    num_layers: int,
    augment_copies: int = 4,
    seed: int = 42,
) -> str:
    if material not in M.MATERIALS:
        raise ValueError(f"Unknown material '{material}'. Known: {list(M.MATERIALS)}")
    if substrate not in M.SUBSTRATES:
        raise ValueError(f"Unknown substrate '{substrate}'. Known: {list(M.SUBSTRATES)}")

    image_dir = os.path.join(data_dir, "train_images")
    mask_dir  = os.path.join(data_dir, "train_semantic_masks")
    for d in (image_dir, mask_dir):
        if not os.path.isdir(d):
            raise FileNotFoundError(
                f"Expected directory not found: {d}\n"
                "See module docstring for the required folder layout."
            )

    print(f"Reading contrasts from {image_dir} ...")
    layer_contrasts, tr_contrasts, bulk_contrasts, background_contrasts = _collect_contrasts(
        image_dir, mask_dir, augment_copies=augment_copies, seed=seed,
    )

    if tr_contrasts.shape[0]         > 0: print(f"  {tr_contrasts.shape[0]:,} tape-residue pixels")
    if bulk_contrasts.shape[0]       > 0: print(f"  {bulk_contrasts.shape[0]:,} bulk pixels")
    if background_contrasts.shape[0] > 0: print(f"  {background_contrasts.shape[0]:,} background pixels")

    print(f"Fitting {num_layers} layer component(s), one Gaussian per labelled layer ...")
    component_dict: dict[str, dict] = {}
    for layer in range(1, num_layers + 1):
        contrasts = layer_contrasts.get(layer, np.empty((0, 3)))
        entry = _fit_single_gaussian(contrasts, str(layer))
        if entry is not None:
            component_dict[str(layer)] = entry

    for key, arr in [("TR", tr_contrasts), ("Bulk", bulk_contrasts), ("Background", background_contrasts)]:
        entry = _fit_single_gaussian(arr, key) if arr.shape[0] > 0 else None
        if entry is not None:
            component_dict[key] = entry
            print(f"  Added {key} component to weights.")

    out_path = os.path.join(M.USER_WEIGHTS_DIR, M.weights_filename(material, substrate))
    os.makedirs(M.USER_WEIGHTS_DIR, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(component_dict, f, indent=4, sort_keys=True)
    print(f"Wrote weights → {out_path}")
    print("The GUI will use these automatically on next launch.")
    return out_path


def main() -> None:
    p = argparse.ArgumentParser(description="Train FlakeFinder weights for a new substrate.")
    p.add_argument("--material", required=True, choices=list(M.MATERIALS))
    p.add_argument("--substrate", required=True, choices=list(M.SUBSTRATES))
    p.add_argument(
        "--data-dir",
        required=True,
        help="Folder containing train_images/ and train_semantic_masks/ subfolders.",
    )
    p.add_argument(
        "--num-layers",
        type=int,
        default=4,
        help="Number of distinct layer thicknesses to fit (default 4: 1L–4L).",
    )
    p.add_argument(
        "--augment-copies",
        type=int,
        default=4,
        help="Augmented variants generated per image to simulate camera variation (default 4).",
    )
    args = p.parse_args()

    train(
        material=args.material,
        substrate=args.substrate,
        data_dir=args.data_dir,
        num_layers=args.num_layers,
        augment_copies=args.augment_copies,
    )


if __name__ == "__main__":
    main()
