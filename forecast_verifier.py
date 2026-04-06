#!/usr/bin/env python3
"""
forecast_verifier.py — Vérifie la précision des prévisions météo par modèle.
Compare les prévisions historiques avec ERA5 (vérité terrain).
Lance via cron quotidien : 0 6 * * *

Zones : 4 cercles concentriques autour de la dernière position InReach.
  - local       : position exacte du bateau
  - near        : ~150 nm dans la direction de navigation
  - regional    : ~400 nm dans la direction de navigation
  - ocean       : ~800 nm dans la direction de navigation
"""

import logging
import math
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "SailTracker/1.0 (forecast-verifier; contact=samuelvisoko@gmail.com)",
    "Accept-Encoding": "gzip, deflate",
})

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "sailtracker.db"
LOG_DIR = BASE_DIR / "logs"

# Zones de repli si aucune position InReach disponible
FALLBACK_ZONES = {
    'local':    (17.9, -62.8),
    'near':     (20.0, -62.8),
    'regional': (22.5, -62.5),
    'ocean':    (26.0, -61.5),
}
ZONE_DISTANCES = {
    'local': 0,
    'near': 150,
    'regional': 400,
    'ocean': 800,
}

HORIZONS = [1, 2, 3, 5, 7]   # jours
MODELS   = ['ecmwf_ifs025', 'gfs_seamless', 'icon_seamless']
REQUEST_TIMEOUT = 60
API_DELAY = 1.0

LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "verifier_cron.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("forecast_verifier")


# ── Géodésie ──────────────────────────────────────────────────────────────────

def offset_position(lat: float, lon: float, distance_nm: float, bearing_deg: float):
    """Déplace un point lat/lon de distance_nm dans la direction bearing_deg."""
    if distance_nm == 0:
        return lat, lon
    d = math.radians(distance_nm / 60.0)
    lat_r  = math.radians(lat)
    lon_r  = math.radians(lon)
    bear_r = math.radians(bearing_deg)
    lat2 = math.asin(
        math.sin(lat_r) * math.cos(d)
        + math.cos(lat_r) * math.sin(d) * math.cos(bear_r)
    )
    lon2 = lon_r + math.atan2(
        math.sin(bear_r) * math.sin(d) * math.cos(lat_r),
        math.cos(d) - math.sin(lat_r) * math.sin(lat2),
    )
    return round(math.degrees(lat2), 3), round(math.degrees(lon2), 3)


def haversine_nm(lat1, lon1, lat2, lon2) -> float:
    R = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(phi1) * math.cos(phi2)
         * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── DB ────────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def get_last_inreach(conn):
    """Retourne (lat, lon, cog, speed) de la dernière position InReach."""
    row = conn.execute(
        """SELECT latitude, longitude, course, speed_knots
           FROM positions WHERE source='inreach'
           ORDER BY timestamp DESC LIMIT 1"""
    ).fetchone()
    if row:
        return (float(row["latitude"]), float(row["longitude"]),
                float(row["course"] or 0), float(row["speed_knots"] or 0))
    return None, None, 0, 0


def build_zones(lat, lon, cog, speed_kts):
    """
    Construit les 4 zones dynamiques autour du bateau.
    Les rings s'étendent dans la direction du cap si le bateau navigue,
    sinon vers le nord (bearing=0).
    """
    bearing = cog if speed_kts > 1.0 else 0.0
    zones = {}
    for name, dist in ZONE_DISTANCES.items():
        z_lat, z_lon = offset_position(lat, lon, dist, bearing)
        zones[name] = (z_lat, z_lon)
    return zones


# ── API ───────────────────────────────────────────────────────────────────────

def fetch_archive(lat, lon, date_str):
    """Fetch ERA5 reanalysis pour une date donnée (vérité terrain)."""
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "kn",
        "start_date": date_str, "end_date": date_str,
    }
    try:
        resp = _SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("Erreur API archive @ (%.2f,%.2f) %s : %s", lat, lon, date_str, e)
        return None


def fetch_forecast(lat, lon, model, previous_day=0):
    """Prévision modèle pour un point et un décalage de jours passés."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "kn",
        "models": model,
        "forecast_days": 1,
        "past_days": previous_day,
    }
    try:
        resp = _SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("Erreur API %s @ (%.2f,%.2f) past_days=%d : %s",
                       model, lat, lon, previous_day, e)
        return None


def extract_day_data(data, target_date_str):
    if not data or "hourly" not in data:
        return None, None
    times  = data["hourly"].get("time", [])
    speeds = data["hourly"].get("wind_speed_10m", [])
    dirs   = data["hourly"].get("wind_direction_10m", [])
    day_speeds, day_dirs = [], []
    for i, t in enumerate(times):
        if t.startswith(target_date_str):
            if i < len(speeds) and speeds[i] is not None:
                day_speeds.append(speeds[i])
            if i < len(dirs) and dirs[i] is not None:
                day_dirs.append(dirs[i])
    return (day_speeds or None), (day_dirs or None)


def angular_error(a, b):
    diff = abs(a - b) % 360
    return min(diff, 360 - diff)


def compute_errors(obs_speeds, obs_dirs, fc_speeds, fc_dirs):
    if not obs_speeds or not fc_speeds:
        return None, None, 0
    n = min(len(obs_speeds), len(fc_speeds))
    wind_errors = [abs(obs_speeds[i] - fc_speeds[i]) for i in range(n)]
    wind_err = float(np.mean(wind_errors))
    dir_err = None
    if obs_dirs and fc_dirs:
        nd = min(len(obs_dirs), len(fc_dirs))
        de = [angular_error(obs_dirs[i], fc_dirs[i]) for i in range(nd)]
        dir_err = float(np.mean(de)) if de else None
    return wind_err, dir_err, n


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    logger.info("=== Forecast Verifier démarré ===")
    conn = get_db()

    lat, lon, cog, speed = get_last_inreach(conn)
    if lat is not None:
        zones = build_zones(lat, lon, cog, speed)
        logger.info("Position InReach : %.3fN %.3fW  COG=%.0f  SOG=%.1f kts",
                    lat, abs(lon), cog, speed)
        for name, (z_lat, z_lon) in zones.items():
            dist = haversine_nm(lat, lon, z_lat, z_lon)
            logger.info("  Zone %-12s : %.3fN %.3fW  (%.0f nm)", name, z_lat, abs(z_lon), dist)
    else:
        zones = FALLBACK_ZONES
        logger.warning("Aucune position InReach — zones de repli utilisées")

    today = datetime.now(timezone.utc).date()
    total_inserts = 0

    for zone_name, (z_lat, z_lon) in zones.items():
        logger.info("Zone %s (%.3f, %.3f)", zone_name, z_lat, z_lon)

        for model in MODELS:
            logger.info("  Modèle : %s", model)

            for horizon_days in HORIZONS:
                target_date     = today - timedelta(days=horizon_days)
                target_date_str = target_date.strftime("%Y-%m-%d")

                obs_data = fetch_archive(z_lat, z_lon, target_date_str)
                time.sleep(API_DELAY)
                if obs_data is None:
                    continue
                obs_speeds, obs_dirs = extract_day_data(obs_data, target_date_str)
                if not obs_speeds:
                    continue

                fc_data = fetch_forecast(z_lat, z_lon, model, previous_day=horizon_days)
                time.sleep(API_DELAY)
                if fc_data is None:
                    continue

                fc_speeds, fc_dirs = extract_day_data(fc_data, target_date_str)
                if not fc_speeds:
                    continue

                wind_err, dir_err, n = compute_errors(obs_speeds, obs_dirs, fc_speeds, fc_dirs)
                if wind_err is None or n < 12:
                    continue

                try:
                    conn.execute(
                        """INSERT OR REPLACE INTO model_accuracy
                           (date, model, zone, forecast_hour,
                            wind_speed_error_avg, wind_dir_error_avg, sample_count,
                            zone_lat, zone_lon)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (target_date_str, model, zone_name, horizon_days * 24,
                         round(wind_err, 3),
                         round(dir_err, 3) if dir_err is not None else None,
                         n, z_lat, z_lon)
                    )
                    total_inserts += 1
                    logger.info("  H+%d : vent=%.2f kts dir=%.1f (n=%d)",
                                horizon_days * 24, wind_err,
                                dir_err if dir_err is not None else 0, n)
                except Exception as e:
                    logger.error("  Erreur INSERT : %s", e)

        conn.commit()

    conn.execute("DELETE FROM model_accuracy WHERE date < date('now', '-90 days')")
    conn.commit()
    conn.close()
    logger.info("=== Forecast Verifier terminé : %d entrées insérées ===", total_inserts)


if __name__ == "__main__":
    main()
