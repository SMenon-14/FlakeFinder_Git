import sys
import os
from pathlib import Path

IS_FROZEN = getattr(sys, 'frozen', False)

if IS_FROZEN:
    APP_ROOT = Path(sys.executable).parent
    USER_DATA_ROOT = Path(os.getenv('LOCALAPPDATA'), Path.home()) / 'FlakeFinder'
else:
    APP_ROOT = Path(__file__).resolve().parent.parent
    USER_DATA_ROOT = APP_ROOT

SRC_DIR = APP_ROOT / 'src'
ASSETS_DIR = APP_ROOT / 'assets'
IMAGES_DIR = USER_DATA_ROOT / 'images'

CONSTANTS_DIR = SRC_DIR / 'constants'
CONFIGS_DIR = CONSTANTS_DIR / 'configs'
THEMES_DIR = CONSTANTS_DIR / 'themes'
AUTOSCAN_DIR = SRC_DIR / 'autoscan'
MODEL_DIR = SRC_DIR / 'autoscan' / 'ffm'
BROWSER_DIR = SRC_DIR / 'browser'
CALIBRATION_DIR = SRC_DIR / 'calibration'
HISTORY_DIR = SRC_DIR / 'history'
MENU_DIR = SRC_DIR / 'menu'
MICROSCOPE_DIR = SRC_DIR / 'microscope'



