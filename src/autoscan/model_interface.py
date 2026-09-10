from src.autoscan.ffm.flakefinder.core import Detector, DetectionRun, WeightsNotFoundError, FlakeResult
import cv2
import numpy as np
import re
import json
import os
from src.paths import CONFIGS_DIR
class ModelInterface:

    def __get_flake_area_in_um(self, mask, scale_factor):
        total_pixel_area = np.count_nonzero(mask)
        area_conversion_factor = scale_factor ** 2
        # Convert the area
        micrometer_area = total_pixel_area * area_conversion_factor
        return micrometer_area


    def __is_valid_flake(self, flake, scale_factor):
        is_confident = flake.confidence >= self.confidence_threshold  
        is_large_enough = self.__get_flake_area_in_um(flake.mask, scale_factor) >= self.size_threshold
        is_not_bulk = flake.layers != 'Bulk'
        return is_confident and is_large_enough and is_not_bulk

    def __convert_px_to_micrometers(self, scale_factor, px_val):
        return scale_factor*px_val

    def __init__(self, material, substrate, confidence_threshold, size_threshold, scanned_date):
        self.material = material
        self.substrate = substrate
        self._detector = Detector()
        self.confidence_threshold = confidence_threshold
        self.size_threshold = size_threshold
        self.scanned_date = scanned_date

    def __load_scale_factor(self, zoom):
        file_path = CONFIGS_DIR / 'calibrated_scale_values.json'
        print(f"Loading scale factor from {file_path} for zoom level {zoom}...")
        print(type(zoom))
        with open(file_path, 'r') as file:
            data = json.load(file)
        print(f"Zoom level: {zoom}, Scale factor: {data['zoom_levels'][zoom]}")
        return data['zoom_levels'][zoom]

    def __get_flake_info_from_filename(self, image_path):
        name = os.path.splitext(os.path.basename(image_path))[0]
        print(f"Extracting flake info from filename: {name}")
        result = re.split(r'[_]', name)
        return result

    def __load_image(self, image_path):
        img = cv2.imread(image_path)
        if img is None:
            return None
        return img

    def _extract_contours(self, flake: FlakeResult) -> list[np.ndarray]:
        """Extract contour point arrays from a flake's binary mask."""
        contours, _ = cv2.findContours(
            flake.mask.astype(np.uint8),
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        return contours

    def detect_flakes(self, image_path: str) -> tuple[list[dict], list[list[np.ndarray]]] | None:
        """Run flake detection on an image.

        Args:
            image_path (str): Path to the image file to analyse.

        Returns:
            A tuple of:
              - list[dict]: One dict per flake (layers, thickness_nm, confidence,
                            size_px, center_xy, max_sidelength_px, min_sidelength_px).
              - list[list[np.ndarray]]: Contours for each flake, parallel to the
                            dict list. Each entry is the list returned by
                            cv2.findContours for that flake.
            Returns None if the image could not be loaded.
        """
        image = self.__load_image(image_path)
        if image is None:
            return None

        try:
            run: DetectionRun = self._detector.detect(image, self.material, self.substrate)
        except WeightsNotFoundError:
            raise

        flake_info = self.__get_flake_info_from_filename(image_path)
        print(f"Extracted flake info from filename: {flake_info}")
        exfoliated_date = flake_info[0]
        chip_number = flake_info[1]
        frame = flake_info[2]
        zoom = flake_info[3]
        sf = self.__load_scale_factor(zoom)

        passing = [f for f in run.flakes if self.__is_valid_flake(f, sf)]

        flake_dicts = [f.to_json_dict() for f in passing]
        contours    = [self._extract_contours(f) for f in passing]

        flake_objects = [
            Flake(
                material = self.material,
                substrate = self.substrate,
                flake_id=f"{chip_number}_{frame}_{i:03d}",
                thickness=d["thickness_nm"],
                layers=d["layers"],
                center_xy=d["center_xy"],
                max_sidelength_px=d["max_sidelength_px"],
                min_sidelength_px=d["min_sidelength_px"],
                frame=frame,
                date_exfoliated=exfoliated_date,
                date_scanned=self.scanned_date,
                scale_factor=sf
            )
            for i, d in enumerate(flake_dicts)
        ]

        return flake_objects, contours
    
class Flake:

    def __convert_px_to_micrometers(self, px_val):
        return self.scale_factor*px_val
    
    def __format_date(self, date_str):
        mm = date_str[0:2]
        dd = date_str[2:4]
        yy = date_str[4:6]
        return f"20{yy}-{mm}-{dd}"
    def to_dict(self):
        return {"flake_id" : self.flake_id,
                "material" : self.material,
                "substrate" : self.substrate,
                "thickness_nm" : self.thickness_info["thickness_nm"],
                "layers" : self.thickness_info["layers"],
                "center_xy" : self.location_info["center_xy"],
                "(row, col)" : self.location_info["(row, col)"],
                "max_sidelength_um" : self.size_info["max_sidelength_um"],
                "min_sidelength_um" : self.size_info["min_sidelength_um"],
                "date_exfoliated" : self.date_info["date_exfoliated"],
                "date_scanned" : self.date_info["date_scanned"],
                "user" : self.user}

    def __init__(self, material, substrate, flake_id, thickness, layers, center_xy, max_sidelength_px, min_sidelength_px, frame, 
                 date_exfoliated, date_scanned, scale_factor):
        self.scale_factor = scale_factor
        self.material = material
        self.substrate = substrate
        self.flake_id = flake_id
        self.thickness_info = {"thickness_nm" : thickness, "layers" : layers}
        self.location_info = {"center_xy" : center_xy, "(row, col)" : frame}
        self.size_info = {"max_sidelength_um" : self.__convert_px_to_micrometers(max_sidelength_px), "min_sidelength_um" : self.__convert_px_to_micrometers(min_sidelength_px)}
        self.date_info = {"date_exfoliated" : self.__format_date(date_exfoliated), "date_scanned" : date_scanned}

    def set_user(self, user):
        self.user = user

    