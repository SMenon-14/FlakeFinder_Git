import cv2
import numpy as np

class FocusCalculator:
    def __init__(self, step_size):
        self.step_size = step_size
        self.s_max_laplacian = 1.0
        self.s_max_brenner = 1.0

    def set_step_size(self, new_step_size):
        old_step_size = self.step_size
        self.step_size = new_step_size
        return old_step_size

    def calculate_parabolic_vertex(self, score_down, score_current, score_up):
        """
        Calculates the signed offset from perfect focus using 3 sequential scores.
        """
        y_minus = score_down
        y_zero = score_current
        y_plus = score_up
        
        denominator = 2 * (y_minus - 2 * y_zero + y_plus)
        if denominator == 0:
            return 0.0
            
        x_peak = (y_minus - y_plus) / denominator
        user_scored_offset = -x_peak
        return max(min(user_scored_offset, 1.0), -1.0)

    def _ensure_grayscale(self, img_array):
        """ Helper to guarantee the incoming NumPy array is single-channel. """
        if len(img_array.shape) == 3:
            return cv2.cvtColor(img_array, cv2.COLOR_BGR2GRAY)
        return img_array

    # =========================================================================
    # LAPLACIAN METRICS
    # =========================================================================

    def get_absolute_focus_score_laplacian(self, img_array):
        """ Calculates focus score using Laplacian variance from a NumPy array. """
        img_gray = self._ensure_grayscale(img_array)
        focus_score = cv2.Laplacian(img_gray, cv2.CV_64F).var()
        return focus_score

    def get_baseline_focus_laplacian(self, in_focus_array):
        """ Expects in_focus_array as a NumPy array. """
        in_focus_score = self.get_absolute_focus_score_laplacian(in_focus_array)
        self.s_max_laplacian = in_focus_score if in_focus_score != 0 else 1.0

    def get_scaled_focus_score_laplacian(self, img_array):
        """ Expects img_array as a NumPy array. """
        if self.s_max_laplacian == 0:
            return 0.0
        return float(self.get_absolute_focus_score_laplacian(img_array) / self.s_max_laplacian)

    def get_directional_focus_score_laplacian(self, img_zd, img_zc, img_zu):
        """ Expects img_zd, img_zc, img_zu all as NumPy arrays. """
        s_zd = self.get_scaled_focus_score_laplacian(img_zd)
        s_zc = self.get_scaled_focus_score_laplacian(img_zc)
        s_zu = self.get_scaled_focus_score_laplacian(img_zu)
        return self.calculate_parabolic_vertex(s_zd, s_zc, s_zu)

    def compute_z_displacement_laplacian(self, img_zd, img_zc, img_zu):
        """ Calculates required physical Z displacement using Laplacian scores from NumPy arrays. """
        s_zd = self.get_scaled_focus_score_laplacian(img_zd)
        s_zc = self.get_scaled_focus_score_laplacian(img_zc)
        s_zu = self.get_scaled_focus_score_laplacian(img_zu)
        
        denominator = 2 * (s_zd - 2 * s_zc + s_zu)
        if denominator == 0:
            return 0.0
            
        x_peak = (s_zd - s_zu) / denominator
        return x_peak * self.step_size

    # =========================================================================
    # BRENNER GRADIENT METRICS
    # =========================================================================

    def get_absolute_focus_score_brenner(self, img_array):
        """ Calculates focus score using the Brenner Gradient from a NumPy array. """
        img_gray = self._ensure_grayscale(img_array)

        # Vectorized Brenner algorithm: shifts 2px horizontally, squares differences, sums totals.
        img_gray_64 = img_gray.astype(np.float64)
        diff = img_gray_64[:, 2:] - img_gray_64[:, :-2]
        focus_score = np.sum(diff ** 2)
        return focus_score

    def get_baseline_focus_brenner(self, in_focus_array):
        """ Expects in_focus_array as a NumPy array. """
        in_focus_score = self.get_absolute_focus_score_brenner(in_focus_array)
        self.s_max_brenner = in_focus_score if in_focus_score != 0 else 1.0

    def get_scaled_focus_score_brenner(self, img_array):
        """ Expects img_array as a NumPy array. """
        if self.s_max_brenner == 0:
            return 0.0
        return float(self.get_absolute_focus_score_brenner(img_array) / self.s_max_brenner)

    def get_directional_focus_score_brenner(self, img_zd, img_zc, img_zu):
        """ Expects img_zd, img_zc, img_zu all as NumPy arrays. """
        s_zd = self.get_scaled_focus_score_brenner(img_zd)
        s_zc = self.get_scaled_focus_score_brenner(img_zc)
        s_zu = self.get_scaled_focus_score_brenner(img_zu)
        return self.calculate_parabolic_vertex(s_zd, s_zc, s_zu)

    def compute_z_displacement_brenner(self, img_zd, img_zc, img_zu):
        """ Calculates required physical Z displacement using Brenner Gradient scores from NumPy arrays. """
        s_zd = self.get_scaled_focus_score_brenner(img_zd)
        s_zc = self.get_scaled_focus_score_brenner(img_zc)
        s_zu = self.get_scaled_focus_score_brenner(img_zu)
        
        denominator = 2 * (s_zd - 2 * s_zc + s_zu)
        if denominator == 0:
            return 0.0
            
        x_peak = (s_zd - s_zu) / denominator
        return x_peak * self.step_size





