import json
import os
import math
from src.paths import CONFIGS_DIR
DEFAULT_CALIBRATION_PATH = CONFIGS_DIR / 'calibrated_scale_values.json'

class CalibrationManager:
    def __init__(self, filepath=DEFAULT_CALIBRATION_PATH):
        self.filepath = filepath
        self.data = self._load_calibration()

    def _load_calibration(self):
        """Loads existing calibration data or ensures the structured root exists."""
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r') as f:
                    content = json.load(f)
                    # Enforce the strict schema layout
                    if "zoom_levels" in content:
                        return content
            except json.JSONDecodeError:
                print(f"Warning: {self.filepath} was corrupt. Re-initializing structure.")
        
        return {"zoom_levels": {}}

    def calculate_um_per_pixel(self, start_coords, end_coords, scale_bar_um):
        """
        Calculates the real-world distance represented by a single pixel.
        Formula: Scale Bar Length (um) / Measured Line Length (pixels)
        """
        x1, y1 = start_coords
        x2, y2 = end_coords
        pixel_distance = math.sqrt((x2 - x1)**2 + (y2 - y1)**2)
        
        if pixel_distance == 0:
            return 0.0
            
        # Calculate um per pixel and round cleanly
        um_per_pixel = scale_bar_um / pixel_distance
        return round(um_per_pixel, 6)

    def save_calibration(self, zoom_value, um_per_pixel):
        """Saves or updates the specific zoom float configuration."""
        # Convert keys to strings for standardized JSON writing
        self.data["zoom_levels"][str(zoom_value)] = um_per_pixel
        
        dir_name = os.path.dirname(self.filepath)
        if dir_name: 
            os.makedirs(dir_name, exist_ok=True)
            
        with open(self.filepath, 'w') as f:
            json.dump(self.data, f, indent=2)