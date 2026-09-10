import sys
import os
import re
import json
import shutil
import argparse
from src.autoscan.model_interface import ModelInterface, Flake
import numpy as np


def __revert_date(formatted_date):
    parts = formatted_date.split('-')
    yy = parts[0][2:4]
    mm = parts[1]
    dd = parts[2]
    return f"{mm}{dd}{yy}"

def main():
    parser = argparse.ArgumentParser(description="Process scanned materials.")

    # 2. Add arguments (match the flags you passed in subprocess)
    parser.add_argument("--file", required=True, help="Path to the file")
    parser.add_argument("--material", required=True, help="Material type")
    parser.add_argument("--substrate", required=True, help="Substrate type")
    parser.add_argument("--confidence", type=float, required=True, help="Confidence threshold")
    parser.add_argument("--size_threshold", type=float, required=True, help="Size threshold")
    parser.add_argument("--scanned_date", required=True, help="Date scanned")
    parser.add_argument("--keep_flakeless_images", required=True, help="Whether or not to store flakeless images")
    parser.add_argument("--user", required=True, help="User who owns chip")

    # 3. Parse the arguments
    args = parser.parse_args()

    # 4. Access the values using the names without dashes
    image_path = args.file
    material = args.material
    substrate = args.substrate
    confidence = args.confidence
    size_threshold = args.size_threshold
    scanned_date = args.scanned_date
    user = args.user
    keep_flakeless_images = (args.keep_flakeless_images).strip().lower() == 'true'

    mi = ModelInterface(material, substrate, confidence, size_threshold, scanned_date)

    filename = os.path.basename(image_path)
    flake_info = re.split(r'[_.]', filename)
    exfoliated_date = flake_info[0]
    chip_number = flake_info[1]
    parent_dir = os.path.dirname(image_path)
    identified_path = os.path.join(
        parent_dir,
        f"ScanningSession_{__revert_date(scanned_date)}",
        exfoliated_date,
        str(chip_number)
    )
    empty_folder_path = os.path.join(
        parent_dir,
        f"ScanningSession_{__revert_date(scanned_date)}",
        f"{exfoliated_date}_empty",
        f"{chip_number}"
    )
    nonempty_dest_path = os.path.join(identified_path, filename)
    empty_dest_path = os.path.join(empty_folder_path, filename)


    flakes, contours = mi.detect_flakes(image_path)
    if len(flakes) != 0:
        os.makedirs(identified_path, exist_ok=True)
        shutil.move(image_path, nonempty_dest_path)
        data_to_save = [(obj.set_user(user), obj.to_dict())[1] for obj in flakes]
        base_path, _ = os.path.splitext(nonempty_dest_path)
        json_path = base_path + ".json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data_to_save, f, indent=4)
        for i, contour in enumerate(contours):
            filename = f"{flakes[i].flake_id}_contour.npy"
            contour_path = os.path.join(identified_path, filename)
            np.save(contour_path, contour)
    else:
        if keep_flakeless_images:
            os.makedirs(empty_folder_path, exist_ok=True)
            shutil.move(image_path, empty_dest_path)
        elif os.path.exists(image_path):
            os.remove(image_path)

if __name__ == "__main__":
    main()

