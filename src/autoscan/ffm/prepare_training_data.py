"""
Convert LabelMe polygon JSONs into the folder layout that train_substrate.py expects.

Usage
-----
    python prepare_training_data.py --input graphene/raw_images --output graphene/training
    python prepare_training_data.py --input hbn/raw_images     --output hbn/training

What it does
------------
For each image in <input>/ that has a matching LabelMe .json:
  - Copies the image  →  <output>/train_images/<name>.png
  - Rasterises every polygon into an 8-bit grayscale mask:
      pixel value = layer count (1, 2, 3, ...)  for flake regions
      pixel value = 200                          for tape residue regions
      pixel value = 0                            for background (unlabelled)
               →  <output>/train_semantic_masks/<name>.png

Label conventions in LabelMe:
  - Flake layers : "1", "2", "3", ...   (integer string)
  - Tape residue : "TR", "tr", "tape", "tape residue"  (case-insensitive)
  - Anything else is silently skipped.
"""

import argparse
import json
import os

import cv2
import numpy as np

# Reserved mask values. Must not clash with layer counts (1–99).
TR_MASK_VALUE   = 200
BULK_MASK_VALUE = 201

_TR_LABELS   = {"tr", "tape", "tape residue", "tape_residue", "taperesidual", "residue"}
_BULK_LABELS = {"bulk", "bulk crystal", "thick", "thick flake"}


def _is_tr_label(label: str) -> bool:
    return label.strip().lower() in _TR_LABELS


def _is_bulk_label(label: str) -> bool:
    return label.strip().lower() in _BULK_LABELS


def process_image(image_path: str, json_path: str, out_image_dir: str, out_mask_dir: str) -> int:
    image = cv2.imread(image_path)
    if image is None:
        print(f"  SKIP — could not read image: {image_path}")
        return 0

    H, W = image.shape[:2]

    with open(json_path) as f:
        ann = json.load(f)

    mask = np.zeros((H, W), dtype=np.uint8)
    n_flake = 0
    n_tr    = 0
    n_bulk  = 0

    ann_h = ann.get("imageHeight", H)
    ann_w = ann.get("imageWidth", W)
    sx = W / ann_w if ann_w != W else 1.0
    sy = H / ann_h if ann_h != H else 1.0

    for shape in ann.get("shapes", []):
        if shape.get("shape_type") != "polygon":
            continue
        label = shape.get("label", "").strip()

        pts = np.array(shape["points"], dtype=np.float32)
        if sx != 1.0 or sy != 1.0:
            pts[:, 0] *= sx
            pts[:, 1] *= sy
        pts_int = pts.astype(np.int32)

        if _is_tr_label(label):
            cv2.fillPoly(mask, [pts_int], color=TR_MASK_VALUE)
            n_tr += 1
        elif _is_bulk_label(label):
            cv2.fillPoly(mask, [pts_int], color=BULK_MASK_VALUE)
            n_bulk += 1
        else:
            layer_label = label
            if layer_label[-1:].lower() == "l" and layer_label[:-1].isdigit():
                layer_label = layer_label[:-1]
            try:
                layer_count = int(layer_label)
            except ValueError:
                continue
            if layer_count < 1:
                continue
            cv2.fillPoly(mask, [pts_int], color=int(layer_count))
            n_flake += 1

    if n_flake == 0 and n_tr == 0 and n_bulk == 0:
        print(f"  SKIP — no usable polygons in {os.path.basename(json_path)}")
        return 0

    stem = os.path.splitext(os.path.basename(image_path))[0]
    cv2.imwrite(os.path.join(out_image_dir, stem + ".png"), image)
    cv2.imwrite(os.path.join(out_mask_dir,  stem + ".png"), mask)

    layer_vals = sorted(set(mask.flatten()) - {0, TR_MASK_VALUE, BULK_MASK_VALUE})
    tags = []
    if n_tr   > 0: tags.append(f"+TR")
    if n_bulk > 0: tags.append(f"+Bulk")
    tag_str = "  " + "  ".join(tags) if tags else ""
    print(f"  OK — {stem}: layers {layer_vals}{tag_str}  ({n_flake} flake, {n_tr} TR, {n_bulk} bulk polygon(s))")
    return 1


def main():
    p = argparse.ArgumentParser(description="LabelMe JSON → training mask converter")
    p.add_argument("--input",  required=True, help="Folder containing images + LabelMe .json files")
    p.add_argument("--output", required=True, help="Output folder (will be created)")
    args = p.parse_args()

    input_dir  = os.path.abspath(args.input)
    output_dir = os.path.abspath(args.output)
    out_image_dir = os.path.join(output_dir, "train_images")
    out_mask_dir  = os.path.join(output_dir, "train_semantic_masks")

    os.makedirs(out_image_dir, exist_ok=True)
    os.makedirs(out_mask_dir,  exist_ok=True)

    image_exts = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
    image_files = sorted(
        f for f in os.listdir(input_dir)
        if os.path.splitext(f)[1].lower() in image_exts
    )

    if not image_files:
        print(f"No image files found in {input_dir}")
        return

    processed = 0
    skipped = 0
    for fname in image_files:
        stem = os.path.splitext(fname)[0]
        image_path = os.path.join(input_dir, fname)
        json_path  = os.path.join(input_dir, stem + ".json")
        if not os.path.exists(json_path):
            print(f"  SKIP — no JSON for {fname}")
            skipped += 1
            continue
        result = process_image(image_path, json_path, out_image_dir, out_mask_dir)
        processed += result
        skipped   += (1 - result)

    print()
    print(f"Done. {processed} image(s) written to {output_dir}")
    print(f"      {skipped} skipped.")
    if processed > 0:
        print()
        print("Next step — train:")
        print(f"  python -m flakefinder.train_substrate \\")
        print(f"      --material <Graphene or hBN> \\")
        print(f"      --substrate <SiO2_285nm or SiO2_90nm> \\")
        print(f"      --data-dir {output_dir} \\")
        print(f"      --num-layers <number>")


if __name__ == "__main__":
    main()
