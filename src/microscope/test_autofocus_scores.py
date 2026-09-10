"""Print Brenner and Laplacian focus scores for one or more saved images.

Use this to see what score range a confirmed in-focus image produces (and how it
compares to blurry ones) without needing the microscope hardware connected.

Usage:
    python src/microscope/test_autofocus_scores.py
    (a file picker opens — select one or more images)
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import tkinter as tk
from tkinter import filedialog

import cv2

from src.microscope.autofocus import FocusCalculator


def select_image_paths() -> list[str]:
    root = tk.Tk()
    root.withdraw()
    paths = filedialog.askopenfilenames(
        title="Select image(s) to score",
        filetypes=[
            ("Images", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff"),
            ("All files", "*.*"),
        ],
    )
    root.destroy()
    return list(paths)


def main() -> int:
    image_paths = select_image_paths()
    if not image_paths:
        print("No images selected.")
        return 1

    af = FocusCalculator(step_size=1.0)
    results = []
    for path in image_paths:
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            print(f"  (skipped, could not read) {path}")
            continue
        brenner = af.get_absolute_focus_score_brenner(img)
        laplacian = af.get_absolute_focus_score_laplacian(img)
        results.append((path, brenner, laplacian))

    if not results:
        print("No readable images.")
        return 1

    name_width = max(len(os.path.basename(p)) for p, _, _ in results)
    print(f"{'image'.ljust(name_width)}  {'brenner':>14}  {'laplacian':>12}")
    for path, brenner, laplacian in results:
        name = os.path.basename(path)
        print(f"{name.ljust(name_width)}  {brenner:>14.1f}  {laplacian:>12.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
