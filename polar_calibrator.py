#!/usr/bin/env python3
"""
polar_calibrator.py — Calibration automatique des polaires POLLEN 1

Logique :
  1. Récupère les positions InReach consécutives (minimum 10 min, maximum 4h d'écart)
  2. Calcule la vitesse réelle entre ces positions (SOG calculé = distance/temps)
  3. Fetche le vent Open-Meteo (archive ou prévision) pour la position médiane
  4. Calcule TWA et TWS
  5. Stocke dans polar_observations
  6. Après 20+ nouvelles observations, calibre les polaires

Cron : 0 * * * *  (toutes les heures)
"""

import logging
import logging.handlers
import math
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "SailTracker/1.0 (polar-calibrator; contact=samuelvisoko@gmail.com)",
    "Accept-Encoding": "gzip, deflate",
})

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from polars import get_polar, update_polars_from_observations

DB_PATH = BASE_DIR / "sailtracker.db"
LOG_PATH = BASE_DIR / "logs" / "polar_calibration.log"
REQUEST_TIMEOUT = 20
API_DELAY = 0.3   # entre appels Open-Meteo

# Seuils
MIN_SOG_KTS = 1.0        # vitesse min pour observation valide
MAX_SOG_KTS = 9.0        # vitesse max (hull speed POLLEN 1 ≈ 7.2 kts + marge)
MIN_TWS_KTS = 3.0        # vent min
MIN_GAP_MIN = 10         # écart min entre 2 positions InReach (minutes)
MAX_GAP_MIN = 240        # écart max (4h)
MAX_CAP_CHANGE_DEG = 30  # changement de cap max entre 2 positions
CALIB_THRESHOLD = 20     # obs avant déclenchement calibration

# ── Logging ──────────────────────────────────────────────────────────────────

LOG_PATH.parent.mkdir(exist_ok=True)
logger = logging.getLogger("polar_calibrator")
logger.setLevel(logging.INFO)
_handler = logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_handler)
logger.propagate = False
# ── DB ────────────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# ── Géodésie ─────────────────────────────────────────────────────────────────

def haversine_nm(lat1, lon1, lat2, lon2) -> float:
    R = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((math.radians(lat2 - lat1)) / 2) ** 2
         + math.cos(phi1) * math.cos(phi2)
         * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing(lat1, lon1, lat2, lon2) -> float:
    dlon = math.radians(lon2 - lon1)
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def compute_twa(cog: float, twd: float) -> float:
    """Angle au vent vrai (0-180°)."""
    angle = (twd - cog + 360) % 360
    return angle if angle <= 180 else 360 - angle


# ── Courants depuis passage_forecasts ──────────────────────────────────────────

def get_current_from_db(lat: float, lon: float, dt: datetime, conn: sqlite3.Connection):
    """
    Cherche le courant le plus proche en temps et position dans passage_forecasts.
    Retourne (current_speed_kts, current_dir_deg) ou (None, None).
    """
    dt_str = dt.strftime("%Y-%m-%dT%H:%M")
    try:
        row = conn.execute(
            """SELECT current_speed_knots, current_direction_deg,
                      ABS(julianday(forecast_time) - julianday(?)) as dt_diff,
                      ((latitude - ?) * (latitude - ?) + (longitude - ?) * (longitude - ?)) as dist2
               FROM passage_forecasts
               WHERE current_speed_knots IS NOT NULL
                 AND ABS(julianday(forecast_time) - julianday(?)) < 0.25
               ORDER BY dt_diff + dist2 * 0.01 ASC
               LIMIT 1""",
            (dt_str, lat, lat, lon, lon, dt_str)
        ).fetchone()
        if row and row["current_speed_knots"] is not None:
            return float(row["current_speed_knots"]), float(row["current_direction_deg"])
    except Exception:
        pass
    return None, None


# ── Open-Meteo ────────────────────────────────────────────────────────────────

def fetch_wind_openmeteo(lat: float, lon: float, dt: datetime):
    """
    Retourne (wind_speed_kts, wind_direction_deg) depuis Open-Meteo.
    Utilise l'API archive pour le passé récent, ou l'API forecast pour
    les données en temps réel.
    """
    date_str = dt.strftime("%Y-%m-%d")
    now_utc = datetime.now(timezone.utc)
    is_past = (now_utc - dt).total_seconds() > 3 * 3600  # > 3h passé

    if is_past:
        url = "https://archive-api.open-meteo.com/v1/archive"
        params = {
            "latitude": round(lat, 3),
            "longitude": round(lon, 3),
            "hourly": "wind_speed_10m,wind_direction_10m",
            "wind_speed_unit": "kn",
            "start_date": date_str,
            "end_date": date_str,
        }
    else:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": round(lat, 3),
            "longitude": round(lon, 3),
            "hourly": "wind_speed_10m,wind_direction_10m",
            "wind_speed_unit": "kn",
            "models": "ecmwf_ifs025",
            "past_days": 1,
            "forecast_days": 1,
        }

    try:
        resp = _SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        speeds = hourly.get("wind_speed_10m", [])
        dirs = hourly.get("wind_direction_10m", [])

        if not times:
            return None, None

        # Trouver l'heure la plus proche
        target_naive = dt.replace(tzinfo=None)
        best_idx, best_diff = 0, float("inf")
        for i, t_str in enumerate(times):
            try:
                t_dt = datetime.fromisoformat(t_str)
                diff = abs((t_dt - target_naive).total_seconds())
                if diff < best_diff:
                    best_diff = diff
                    best_idx = i
            except Exception:
                continue

        if best_diff > 3600:  # > 1h d'écart → pas fiable
            return None, None

        spd = speeds[best_idx] if best_idx < len(speeds) else None
        drn = dirs[best_idx] if best_idx < len(dirs) else None
        return spd, drn

    except Exception as e:
        logger.debug("Open-Meteo error (%.3f, %.3f) %s: %s", lat, lon, date_str, e)
        return None, None


# ── Traitement des paires de positions ───────────────────────────────────────

def process_position_pair(pos1: dict, pos2: dict) -> dict | None:
    """
    Analyse une paire de positions consécutives InReach et retourne
    un dict d'observation ou None si invalide.
    """
    lat1, lon1 = pos1["latitude"], pos1["longitude"]
    lat2, lon2 = pos2["latitude"], pos2["longitude"]

    try:
        t1 = datetime.fromisoformat(pos1["timestamp"].replace("Z", "+00:00"))
        if t1.tzinfo is None: t1 = t1.replace(tzinfo=timezone.utc)
        t2 = datetime.fromisoformat(pos2["timestamp"].replace("Z", "+00:00"))
        if t2.tzinfo is None: t2 = t2.replace(tzinfo=timezone.utc)
    except Exception:
        return None

    dt_min = (t2 - t1).total_seconds() / 60
    if dt_min < MIN_GAP_MIN or dt_min > MAX_GAP_MIN:
        return None

    dt_h = dt_min / 60
    dist_nm = haversine_nm(lat1, lon1, lat2, lon2)
    sog_kts = dist_nm / dt_h  # vitesse calculée entre les 2 positions

    if not (MIN_SOG_KTS <= sog_kts <= MAX_SOG_KTS):
        return None

    cog = bearing(lat1, lon1, lat2, lon2)

    # Vérif changement de cap (si pos1 a un cog connu)
    if pos1.get("course") is not None:
        cap_change = abs((cog - pos1["course"] + 180) % 360 - 180)
        if cap_change > MAX_CAP_CHANGE_DEG:
            return None

    # Point médian et temps médian pour fetch vent
    mid_lat = (lat1 + lat2) / 2
    mid_lon = (lon1 + lon2) / 2
    mid_time = t1 + timedelta(hours=dt_h / 2)

    return {
        "lat": mid_lat,
        "lon": mid_lon,
        "mid_time": mid_time,
        "timestamp": t1.isoformat(),
        "sog_kts": round(sog_kts, 3),
        "cog": round(cog, 1),
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def run_calibration():
    logger.info("=== Démarrage calibration polaires ===")
    db = get_db()
    c = db.cursor()

    # Positions InReach déjà traitées (identifiées par timestamp)
    try:
        already_done = set(
            r[0] for r in c.execute(
                "SELECT timestamp FROM polar_observations"
            ).fetchall()
        )
    except Exception as e:
        logger.error("Erreur lecture polar_observations: %s", e)
        db.close()
        return

    # Récupère les positions InReach (source='inreach'), triées par timestamp
    try:
        positions = c.execute("""
            SELECT id, timestamp, latitude, longitude, speed_knots, course
            FROM positions
            WHERE source = 'inreach'
            ORDER BY timestamp ASC
        """).fetchall()
    except Exception as e:
        logger.error("Erreur lecture positions: %s", e)
        db.close()
        return

    positions = list(positions)
    logger.info("%d positions InReach au total", len(positions))

    if len(positions) < 2:
        logger.info("Pas assez de positions InReach")
        db.close()
        return

    inserted = 0
    api_calls = 0

    for i in range(len(positions) - 1):
        pos1 = dict(positions[i])
        pos2 = dict(positions[i + 1])

        # Déjà traité ?
        if pos1["timestamp"] in already_done:
            continue

        obs = process_position_pair(pos1, pos2)
        if obs is None:
            continue

        # Fetch vent Open-Meteo
        time.sleep(API_DELAY)
        tws_kts, twd = fetch_wind_openmeteo(obs["lat"], obs["lon"], obs["mid_time"])
        api_calls += 1

        if tws_kts is None or twd is None:
            logger.debug("Pas de vent pour %s (%.3f, %.3f)", obs["timestamp"], obs["lat"], obs["lon"])
            continue

        if tws_kts < MIN_TWS_KTS:
            logger.debug("Vent trop faible (%.1f kts) pour %s", tws_kts, obs["timestamp"])
            continue

        twa = compute_twa(obs["cog"], twd)

        # Correction courant : STW = SOG - composante du courant dans l'axe du cap
        current_speed_kts, current_dir_deg = get_current_from_db(
            obs["lat"], obs["lon"], obs["mid_time"], db
        )
        if current_speed_kts and current_dir_deg is not None:
            angle_rad = math.radians(current_dir_deg - obs["cog"])
            along_track = current_speed_kts * math.cos(angle_rad)
            stw = max(0.1, obs["sog_kts"] - along_track)
        else:
            stw = obs["sog_kts"]
            current_speed_kts, current_dir_deg = None, None

        try:
            c.execute("""
                INSERT OR IGNORE INTO polar_observations
                    (timestamp, latitude, longitude,
                     sog_kts, cog_deg,
                     tws_kts, twd_deg, twa_deg,
                     current_speed_kts, current_dir_deg,
                     stw_kts, is_valid)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """, (
                obs["timestamp"],
                round(obs["lat"], 5), round(obs["lon"], 5),
                obs["sog_kts"], obs["cog"],
                round(tws_kts, 2), round(twd, 1), round(twa, 1),
                None, None,
                round(stw, 3),
            ))
            if c.rowcount > 0:
                inserted += 1
                already_done.add(obs["timestamp"])
        except Exception as e:
            logger.warning("Erreur insertion: %s", e)

    db.commit()
    logger.info("%d nouvelles observations insérées (%d appels API)", inserted, api_calls)

    # Compte total des observations valides
    total_obs = c.execute(
        "SELECT COUNT(*) FROM polar_observations WHERE is_valid=1"
    ).fetchone()[0]
    logger.info("Total observations valides : %d", total_obs)

    # Calibration si on a suffisamment d'observations
    if total_obs >= CALIB_THRESHOLD:
        polar = get_polar()
        updated = update_polars_from_observations(db, polar, min_obs=5)
        if updated > 0:
            logger.info("Polaires calibrées : %d cases mises à jour (total %d obs)", updated, total_obs)
        else:
            logger.info("Calibration : pas assez d'observations par case (total=%d)", total_obs)
    else:
        logger.info("Calibration différée : %d/%d observations requises", total_obs, CALIB_THRESHOLD)

    db.close()
    logger.info("=== Calibration terminée ===")


if __name__ == "__main__":
    run_calibration()
