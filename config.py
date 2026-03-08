"""
Shared configuration constants for SailTracker.
Import from here to avoid duplication across modules.
"""
from pathlib import Path
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = BASE_DIR / "sailtracker.db"
STATIC_DIR = BASE_DIR / "static"
TEMPLATE_DIR = BASE_DIR / "templates"
LOG_DIR = BASE_DIR / "logs"
GRIB_CACHE_DIR = STATIC_DIR / "grib_cache"

FLASK_HOST = os.getenv("FLASK_HOST", "127.0.0.1")
FLASK_PORT = int(os.getenv("FLASK_PORT", "8085"))

VERIF_ZONES = {
    'cabo_verde':     (16.9, -25.0),
    'mid_atlantic':   (15.0, -40.0),
    'caribbean_east': (13.5, -55.0),
    'caribbean_west': (17.9, -62.8),
}

WIND_MODELS = ['ecmwf_ifs025', 'gfs_seamless', 'icon_seamless']
BOAT_SPEED_DEFAULT = 6.0
