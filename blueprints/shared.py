"""
shared.py — Utilitaires partagés entre tous les blueprints.
"""
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import os

from dotenv import load_dotenv
from flask_login import UserMixin

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = BASE_DIR / "sailtracker.db"
STATIC_DIR = BASE_DIR / "static"
GRIB_CACHE_DIR = STATIC_DIR / "grib_cache"

# Zones de vérification météo
VERIF_ZONES_ORDERED = ['local', 'near', 'regional', 'ocean']
VERIF_ZONE_LABELS = {
    'local':    'Local',
    'near':     '~150 nm',
    'regional': '~400 nm',
    'ocean':    '~800 nm',
}
VERIF_ZONES = {
    'local':    (17.9, -62.8),
    'near':     (20.0, -62.8),
    'regional': (22.5, -62.5),
    'ocean':    (26.0, -61.5),
}


class User(UserMixin):
    """Modèle utilisateur Flask-Login."""
    def __init__(self, id, username, email, boat_name, boat_type, is_admin, telegram_chat_id=None):
        self.id = id
        self.username = username
        self.email = email
        self.boat_name = boat_name
        self.boat_type = boat_type
        self.is_admin = bool(is_admin)
        self.telegram_chat_id = telegram_chat_id


def get_db() -> sqlite3.Connection:
    """Ouvre une connexion SQLite avec accès par nom de colonne."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance orthodromique en milles nautiques."""
    R = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def minutes_ago(ts_str: str | None) -> int | None:
    """Retourne le nombre de minutes écoulées depuis ts_str (ISO)."""
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - ts).total_seconds() / 60)
    except Exception:
        return None


def great_circle_waypoints(lat1, lon1, name1, lat2, lon2, name2, spacing_nm=250.0):
    """Génère des waypoints intermédiaires sur un arc de grand cercle."""
    total = haversine_nm(lat1, lon1, lat2, lon2)
    n_seg = max(1, round(total / spacing_nm))
    la1r, lo1r = math.radians(lat1), math.radians(lon1)
    la2r, lo2r = math.radians(lat2), math.radians(lon2)
    d = 2 * math.asin(math.sqrt(
        math.sin((la2r - la1r) / 2)**2 +
        math.cos(la1r) * math.cos(la2r) * math.sin((lo2r - lo1r) / 2)**2
    ))
    wps = [{"lat": lat1, "lon": lon1, "name": name1}]
    for i in range(1, n_seg):
        f = i / n_seg
        if abs(d) < 1e-10:
            wlat, wlon = lat1, lon1
        else:
            A = math.sin((1 - f) * d) / math.sin(d)
            B = math.sin(f * d) / math.sin(d)
            x = A * math.cos(la1r) * math.cos(lo1r) + B * math.cos(la2r) * math.cos(lo2r)
            y = A * math.cos(la1r) * math.sin(lo1r) + B * math.cos(la2r) * math.sin(lo2r)
            z = A * math.sin(la1r) + B * math.sin(la2r)
            wlat = math.degrees(math.atan2(z, math.sqrt(x**2 + y**2)))
            wlon = math.degrees(math.atan2(y, x))
        wps.append({"lat": round(wlat, 4), "lon": round(wlon, 4), "name": f"WP{i}"})
    wps.append({"lat": lat2, "lon": lon2, "name": name2})
    return wps
