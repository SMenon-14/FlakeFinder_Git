"""ToupCam SDK dynamic loader + camera/SDK constants and color presets."""
import os
import ctypes
import importlib.util
from src.paths import CONFIGS_DIR
_TOUPCAM_SDK_MODULE = None
_TOUPCAM_SDK_DLL_DIR_HANDLE = None


def _load_toupcam_sdk_module():
    global _TOUPCAM_SDK_MODULE, _TOUPCAM_SDK_DLL_DIR_HANDLE
    if _TOUPCAM_SDK_MODULE is not None:
        return _TOUPCAM_SDK_MODULE

    sdk_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sdk")
    sdk_python_dir = os.path.join(sdk_root, "python")
    sdk_module_path = os.path.join(sdk_python_dir, "toupcam.py")
    if not os.path.isfile(sdk_module_path):
        raise FileNotFoundError(
            f"ToupCam Python wrapper not found: {sdk_module_path}"
        )

    if os.name == "nt":
        sdk_win_dir = os.path.join(
            sdk_root,
            "win",
            "x64" if ctypes.sizeof(ctypes.c_void_p) == 8 else "x86",
        )
        if not os.path.isdir(sdk_win_dir):
            raise FileNotFoundError(
                f"ToupCam DLL directory not found: {sdk_win_dir}"
            )
        os.environ["PATH"] = sdk_win_dir + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory") and _TOUPCAM_SDK_DLL_DIR_HANDLE is None:
            _TOUPCAM_SDK_DLL_DIR_HANDLE = os.add_dll_directory(sdk_win_dir)

    spec = importlib.util.spec_from_file_location("toupcam_sdk_wrapper", sdk_module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load ToupCam wrapper from {sdk_module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _TOUPCAM_SDK_MODULE = module
    return module

SDK_PREVIEW_INDEX = 1
SDK_REALTIME_MODE = 1
SDK_USE_MAX_SPEED = True
SDK_FRAME_WAIT_TIMEOUT_S = 2.0
SDK_FRAME_POLL_DELAY_S = 0.05
SDK_DEFAULT_WB_TEMP = 5726
SDK_DEFAULT_WB_TINT = 1173
SDK_DEFAULT_HUE = 0
SDK_DEFAULT_SATURATION = 138
SDK_DEFAULT_BRIGHTNESS = 0
SDK_DEFAULT_CONTRAST = 0
SDK_DEFAULT_GAMMA = 100
SDK_DEFAULT_EXPOSURE_US = 10000
SDK_HUE_MIN = -180
SDK_HUE_MAX = 180
SDK_SATURATION_MIN = 0
SDK_SATURATION_MAX = 255
SDK_BRIGHTNESS_MIN = -255
SDK_BRIGHTNESS_MAX = 255
SDK_CONTRAST_MIN = -255
SDK_CONTRAST_MAX = 255
SDK_GAMMA_MIN = 20
SDK_GAMMA_MAX = 180
SDK_EXPOSURE_MIN_US = 100
SDK_EXPOSURE_MAX_US = 1000
# Tk rendering is intentionally capped below 30 FPS. The SDK may capture
# faster, but the preview pipeline keeps only the newest frame.
CAMERA_PREVIEW_REFRESH_MS = 34
CAMERA_PREVIEW_MAX_DIM = 960

CAMERA_COLOR_PRESETS_PATH = CONFIGS_DIR / 'camera_color_presets.json'
CAMERA_COLOR_PRESET_KEYS = ("brightness", "contrast", "saturation", "gamma", "hue")
DEFAULT_CAMERA_COLOR_PRESETS = {
    "Graphene": {
        "brightness": -53,
        "contrast": -13,
        "saturation": 153,
        "gamma": 100,
        "hue": 9,
    },
}
