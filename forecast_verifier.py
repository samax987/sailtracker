#!/usr/bin/env python3
"""
forecast_verifier.py — Vérifie la précision des prévisions météo par modèle.
Compare les prévisions historiques avec les "observations" (analyses récentes).
Lance via cron quotidien : 0 6 * * *
"""

import json
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

ZONES = {
    'cabo_verde':     (16.9, -25.0),
    'mid_atlantic':   (15.0, -40.0),
    'caribbean_east': (13.5, -55.0),
    'caribbean_west': (17.9, -62.8),
}

HORIZONS = [1, 2, 3, 5, 7]  # jours
MODELS = ['ecmwf_ifs025', 'gfs_seamless', 'icon_seamless']
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


def angular_error(a, b):
    """Erreur angulaire entre deux angles en degrés (gestion passage 0°/360°)."""
    diff = abs(a - b) % 360
    return min(diff, 360 - diff)


def fetch_archive(lat, lon, date_str):
    """Fetch ERA5 reanalysis (vérité terrain) pour une date donnée."""
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "kn",
        "start_date": date_str,
        "end_date": date_str,
    }
    try:
        resp = _SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("Erreur API archive @ (%.1f,%.1f) %s : %s", lat, lon, date_str, e)
        return None


def fetch_forecast(lat, lon, model, previous_day=0):
    """Récupère les prévisions pour un modèle et jour donné."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
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
        logger.warning("Erreur API %s @ (%.1f,%.1f) previous_day=%d : %s", model, lat, lon, previous_day, e)
        return None


def extract_day_data(data, target_date_str):
    """Extrait les données horaires pour une date donnée (format YYYY-MM-DD)."""
    if not data or "hourly" not in data:
        return None, None
    times = data["hourly"].get("time", [])
    speeds = data["hourly"].get("wind_speed_10m", [])
    dirs = data["hourly"].get("wind_direction_10m", [])

    day_speeds = []
    day_dirs = []
    for i, t in enumerate(times):
        if t.startswith(target_date_str):
            if i < len(speeds) and speeds[i] is not None:
                day_speeds.append(speeds[i])
            if i < len(dirs) and dirs[i] is not None:
                day_dirs.append(dirs[i])

    return day_speeds if day_speeds else None, day_dirs if day_dirs else None


def compute_errors(obs_speeds, obs_dirs, fc_speeds, fc_dirs):
    """Calcule les erreurs moyennes de vent et direction."""
    if not obs_speeds or not fc_speeds:
        return None, None, 0

    n = min(len(obs_speeds), len(fc_speeds))
    if n == 0:
        return None, None, 0

    wind_errors = [abs(obs_speeds[i] - fc_speeds[i]) for i in range(n)]
    wind_error_avg = float(np.mean(wind_errors))

    if obs_dirs and fc_dirs:
        n_dir = min(len(obs_dirs), len(fc_dirs))
        dir_errors = [angular_error(obs_dirs[i], fc_dirs[i]) for i in range(n_dir)]
        dir_error_avg = float(np.mean(dir_errors)) if dir_errors else None
    else:
        dir_error_avg = None

    return wind_error_avg, dir_error_avg, n


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def main():
    logger.info("=== Forecast Verifier démarré ===")
    conn = get_db()

    today = datetime.now(timezone.utc).date()
    # On vérifie la veille (données complètes)
    verify_date = today - timedelta(days=1)
    verify_date_str = verify_date.strftime("%Y-%m-%d")

    total_inserts = 0

    for zone_name, (lat, lon) in ZONES.items():
        logger.info("Zone : %s (%.1f, %.1f)", zone_name, lat, lon)

        # Observations ERA5 = vérité terrain (une seule fois par zone, indépendant du modèle)
        for model in MODELS:
            logger.info("  Modèle : %s", model)

            for horizon_days in HORIZONS:
                # Chaque horizon compare ERA5 vs prévision pour un jour différent
                # H+24 → compare jour J-1, H+48 → J-2, etc.
                target_date = today - timedelta(days=horizon_days)
                target_date_str = target_date.strftime("%Y-%m-%d")

                obs_data = fetch_archive(lat, lon, target_date_str)
                time.sleep(API_DELAY)
                if obs_data is None:
                    continue
                obs_speeds, obs_dirs = extract_day_data(obs_data, target_date_str)
                if not obs_speeds:
                    continue

                fc_data = fetch_forecast(lat, lon, model, previous_day=horizon_days)
                time.sleep(API_DELAY)

                if fc_data is None:
                    continue

                fc_speeds, fc_dirs = extract_day_data(fc_data, target_date_str)
                if not fc_speeds:
                    continue

                wind_err, dir_err, n = compute_errors(obs_speeds, obs_dirs, fc_speeds, fc_dirs)

                if wind_err is None or n < 12:  # Au moins 12h de données
                    logger.debug("  Données insuffisantes pour H+%d (n=%d)", horizon_days * 24, n)
                    continue

                forecast_hour = horizon_days * 24

                try:
                    conn.execute(
                        """INSERT OR REPLACE INTO model_accuracy
                           (date, model, zone, forecast_hour, wind_speed_error_avg, wind_dir_error_avg, sample_count)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (target_date_str, model, zone_name, forecast_hour,
                         round(wind_err, 3), round(dir_err, 3) if dir_err is not None else None, n)
                    )
                    total_inserts += 1
                    logger.info("  H+%d : erreur vent=%.2f kts dir=%.1f° (n=%d)",
                               forecast_hour, wind_err,
                               dir_err if dir_err is not None else 0, n)
                except Exception as e:
                    logger.error("  Erreur INSERT : %s", e)

        conn.commit()

    # Nettoyage des données > 90 jours
    conn.execute(
        "DELETE FROM model_accuracy WHERE date < date('now', '-90 days')"
    )
    conn.commit()
    conn.close()

    logger.info("=== Forecast Verifier terminé : %d entrées insérées ===", total_inserts)


if __name__ == "__main__":
    main()
