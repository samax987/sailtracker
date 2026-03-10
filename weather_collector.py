#!/usr/bin/env python3
"""
weather_collector.py — Collecteur météo/océan
Exécuté par cron toutes les 3 heures.
Sources : Open-Meteo (vent + vagues) et Copernicus Marine (courants).
"""

import logging
import logging.handlers
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import time

import requests
from dotenv import load_dotenv

# Session HTTP partagée — réutilise les connexions SSL, évite le rate-limiting
_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "SailTracker/1.0 (weather-collector; contact=samuelvisoko@gmail.com)",
    "Accept-Encoding": "gzip, deflate",
})

# =============================================================================
# Configuration
# =============================================================================

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = BASE_DIR / "sailtracker.db"

COPERNICUS_USER = os.getenv("COPERNICUS_USER", "")
COPERNICUS_PASS = os.getenv("COPERNICUS_PASS", "")

# Timeouts HTTP
HTTP_TIMEOUT = 30  # secondes


def fetch_with_retry(url, params, max_attempts=3):
    """GET HTTP avec retry exponentiel (30s/60s/120s) pour erreurs SSL/réseau."""
    delays = [30, 60, 120]
    for attempt in range(max_attempts):
        try:
            resp = _SESSION.get(url, params=params, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            if attempt < max_attempts - 1:
                delay = delays[attempt]
                logger.warning(
                    "Tentative %d/%d échouée (%s), retry dans %ds",
                    attempt + 1, max_attempts, e, delay,
                )
                time.sleep(delay)
            else:
                logger.error("Échec après %d tentatives : %s", max_attempts, e)
                raise

# =============================================================================
# Logging avec rotation
# =============================================================================

def setup_logging() -> logging.Logger:
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)

    logger = logging.getLogger("weather_collector")
    logger.setLevel(logging.INFO)

    handler = logging.handlers.RotatingFileHandler(
        log_dir / "weather.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    return logger


logger = setup_logging()

# =============================================================================
# Base de données
# =============================================================================

def get_latest_position() -> tuple[float, float] | None:
    """Récupère la dernière position connue depuis SQLite."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.execute(
        "SELECT latitude, longitude FROM positions ORDER BY timestamp DESC LIMIT 1"
    )
    row = c.fetchone()
    conn.close()
    if row:
        return float(row[0]), float(row[1])
    return None


def save_weather_snapshot(
    lat: float,
    lon: float,
    wind: dict | None,
    waves: dict | None,
    currents: dict | None,
) -> None:
    """Insère un snapshot météo complet dans weather_snapshots."""
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO weather_snapshots (
            collected_at, latitude, longitude,
            wind_speed_kmh, wind_direction_deg, wind_gusts_kmh,
            wave_height_m, wave_direction_deg, wave_period_s,
            swell_height_m, swell_direction_deg, swell_period_s,
            current_speed_knots, current_direction_deg
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now, lat, lon,
            wind.get("speed_kmh") if wind else None,
            wind.get("direction_deg") if wind else None,
            wind.get("gusts_kmh") if wind else None,
            waves.get("height_m") if waves else None,
            waves.get("direction_deg") if waves else None,
            waves.get("period_s") if waves else None,
            waves.get("swell_height_m") if waves else None,
            waves.get("swell_direction_deg") if waves else None,
            waves.get("swell_period_s") if waves else None,
            currents.get("speed_knots") if currents else None,
            currents.get("direction_deg") if currents else None,
        ),
    )
    conn.commit()
    conn.close()
    logger.info("Snapshot météo enregistré pour (%.4f, %.4f)", lat, lon)


def save_forecasts(collected_at: str, data_type: str, forecasts: list[dict]) -> None:
    """
    Insère les prévisions horaires dans weather_forecasts.
    data_type : 'wind' ou 'wave'
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()

    # Supprimer les anciennes prévisions du même type collectées avant maintenant
    c.execute(
        "DELETE FROM weather_forecasts WHERE data_type = ? AND collected_at < ?",
        (data_type, collected_at),
    )

    rows = []
    for f in forecasts:
        if data_type == "wind":
            rows.append((
                collected_at,
                f.get("time"),
                data_type,
                f.get("speed_kmh"),
                f.get("direction_deg"),
                f.get("gusts_kmh"),
            ))
        else:  # wave
            rows.append((
                collected_at,
                f.get("time"),
                data_type,
                f.get("height_m"),
                f.get("direction_deg"),
                f.get("period_s"),
            ))

    c.executemany(
        """
        INSERT INTO weather_forecasts
            (collected_at, forecast_time, data_type, value1, value2, value3)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()
    logger.info("%d prévisions '%s' enregistrées", len(rows), data_type)


# =============================================================================
# Open-Meteo Weather API (vent)
# =============================================================================

def fetch_wind(lat: float, lon: float) -> tuple[dict | None, list[dict]]:
    """
    Appelle Open-Meteo Weather API.
    Retourne (données_actuelles, prévisions_horaires).
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
        "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
        "wind_speed_unit": "kmh",
        "forecast_days": 3,
        "timezone": "UTC",
    }

    try:
        resp = fetch_with_retry(url, params)
        data = resp.json()
    except requests.RequestException as e:
        logger.error("Échec Open-Meteo Weather après 3 tentatives : %s", e)
        return None, []

    # Données actuelles
    current = data.get("current", {})
    current_data = {
        "speed_kmh": current.get("wind_speed_10m"),
        "direction_deg": current.get("wind_direction_10m"),
        "gusts_kmh": current.get("wind_gusts_10m"),
    }

    # Prévisions horaires
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    speeds = hourly.get("wind_speed_10m", [])
    dirs = hourly.get("wind_direction_10m", [])
    gusts = hourly.get("wind_gusts_10m", [])

    forecasts = []
    for i, t in enumerate(times):
        forecasts.append({
            "time": t + ":00Z",  # Open-Meteo renvoie "2025-02-22T15:00"
            "speed_kmh": speeds[i] if i < len(speeds) else None,
            "direction_deg": dirs[i] if i < len(dirs) else None,
            "gusts_kmh": gusts[i] if i < len(gusts) else None,
        })

    logger.info(
        "Vent actuel : %.1f km/h dir %d° (rafales %.1f km/h)",
        current_data.get("speed_kmh") or 0,
        int(current_data.get("direction_deg") or 0),
        current_data.get("gusts_kmh") or 0,
    )
    return current_data, forecasts


# =============================================================================
# Open-Meteo Marine API (vagues + houle)
# =============================================================================

def fetch_waves(lat: float, lon: float) -> tuple[dict | None, list[dict]]:
    """
    Appelle Open-Meteo Marine API.
    Retourne (données_actuelles, prévisions_horaires).
    """
    url = "https://marine-api.open-meteo.com/v1/marine"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": (
            "wave_height,wave_direction,wave_period,"
            "swell_wave_height,swell_wave_direction,swell_wave_period"
        ),
        "hourly": (
            "wave_height,wave_direction,wave_period,"
            "swell_wave_height,swell_wave_direction,swell_wave_period"
        ),
        "forecast_days": 3,
        "timezone": "UTC",
    }

    try:
        resp = fetch_with_retry(url, params)
        data = resp.json()
    except requests.RequestException as e:
        logger.error("Échec Open-Meteo Marine après 3 tentatives : %s", e)
        return None, []

    current = data.get("current", {})
    current_data = {
        "height_m": current.get("wave_height"),
        "direction_deg": current.get("wave_direction"),
        "period_s": current.get("wave_period"),
        "swell_height_m": current.get("swell_wave_height"),
        "swell_direction_deg": current.get("swell_wave_direction"),
        "swell_period_s": current.get("swell_wave_period"),
    }

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    heights = hourly.get("wave_height", [])
    dirs = hourly.get("wave_direction", [])
    periods = hourly.get("wave_period", [])

    forecasts = []
    for i, t in enumerate(times):
        forecasts.append({
            "time": t + ":00Z",
            "height_m": heights[i] if i < len(heights) else None,
            "direction_deg": dirs[i] if i < len(dirs) else None,
            "period_s": periods[i] if i < len(periods) else None,
        })

    logger.info(
        "Vagues actuelles : %.1f m dir %d° période %.1f s",
        current_data.get("height_m") or 0,
        int(current_data.get("direction_deg") or 0),
        current_data.get("period_s") or 0,
    )
    return current_data, forecasts


# =============================================================================
# Copernicus Marine (courants de surface)
# =============================================================================

def uv_to_speed_direction(uo: float, vo: float) -> tuple[float, float]:
    """
    Convertit les composantes de vitesse (m/s) en nœuds + direction (degrés).
    uo = composante Est-Ouest, vo = composante Nord-Sud.
    """
    speed_ms = math.sqrt(uo**2 + vo**2)
    speed_knots = speed_ms * 1.944  # 1 m/s = 1.944 nœuds

    # Direction : d'où vient le courant (convention météo)
    direction_rad = math.atan2(uo, vo)
    direction_deg = math.degrees(direction_rad) % 360

    return speed_knots, direction_deg


def fetch_currents(lat: float, lon: float) -> dict | None:
    """
    Récupère les courants de surface via Copernicus Marine Service.
    Utilise la bibliothèque copernicusmarine.
    Retourne None en cas d'échec (fallback gracieux).
    """
    if not COPERNICUS_USER or COPERNICUS_USER.startswith("REMPLACE"):
        logger.warning("Identifiants Copernicus non configurés, courants ignorés")
        return None

    try:
        import copernicusmarine  # Import ici pour ne pas bloquer si non installé

        # Bounding box ±2° autour de la position
        lon_min = round(lon - 2.0, 3)
        lon_max = round(lon + 2.0, 3)
        lat_min = round(lat - 2.0, 3)
        lat_max = round(lat + 2.0, 3)

        now = datetime.now(timezone.utc)
        date_start = now.strftime("%Y-%m-%dT%H:00:00")

        logger.info(
            "Téléchargement Copernicus Marine (%.2f–%.2f°N, %.2f–%.2f°E)...",
            lat_min, lat_max, lon_min, lon_max,
        )

        ds = copernicusmarine.open_dataset(
            dataset_id="cmems_mod_glo_phy_anfc_0.083deg_PT1H-m",
            variables=["uo", "vo"],
            minimum_longitude=lon_min,
            maximum_longitude=lon_max,
            minimum_latitude=lat_min,
            maximum_latitude=lat_max,
            minimum_depth=0,
            maximum_depth=1,
            start_datetime=date_start,
            username=COPERNICUS_USER,
            password=COPERNICUS_PASS,
        )

        # Sélectionner la valeur la plus proche de la position exacte
        ds_point = ds.sel(
            latitude=lat, longitude=lon, method="nearest"
        ).isel(depth=0, time=0)

        uo = float(ds_point["uo"].values)
        vo = float(ds_point["vo"].values)

        speed_knots, direction_deg = uv_to_speed_direction(uo, vo)

        logger.info(
            "Courant : %.2f nœuds direction %.0f°",
            speed_knots, direction_deg,
        )

        return {
            "speed_knots": round(speed_knots, 2),
            "direction_deg": round(direction_deg, 1),
        }

    except ImportError:
        logger.error("Bibliothèque copernicusmarine non installée")
        return None
    except Exception as e:  # noqa: BLE001
        logger.error("Erreur Copernicus Marine : %s", e)
        return None


# =============================================================================
# Point d'entrée principal
# =============================================================================

def main() -> None:
    logger.info("=== Collecte météo/océan démarrée ===")

    # 1. Récupérer la dernière position connue
    position = get_latest_position()
    if position is None:
        logger.warning(
            "Aucune position dans la base de données. "
            "En attente de données AIS. Arrêt."
        )
        return

    lat, lon = position
    logger.info("Position actuelle : %.4f°N, %.4f°E", lat, lon)

    # 2. Collecter vent (Open-Meteo Weather)
    wind_current, wind_forecasts = None, []
    try:
        wind_current, wind_forecasts = fetch_wind(lat, lon)
    except Exception as e:  # noqa: BLE001
        logger.error("Échec collecte vent : %s", e)

    # 3. Collecter vagues/houle (Open-Meteo Marine)
    wave_current, wave_forecasts = None, []
    try:
        wave_current, wave_forecasts = fetch_waves(lat, lon)
    except Exception as e:  # noqa: BLE001
        logger.error("Échec collecte vagues : %s", e)

    # 4. Collecter courants (Copernicus) — fallback silencieux
    currents = None
    try:
        currents = fetch_currents(lat, lon)
    except Exception as e:  # noqa: BLE001
        logger.error("Échec collecte courants Copernicus : %s", e)

    # Fusionner wave_current et swell dans un seul dict pour le snapshot
    waves_full = None
    if wave_current:
        waves_full = {
            "height_m": wave_current.get("height_m"),
            "direction_deg": wave_current.get("direction_deg"),
            "period_s": wave_current.get("period_s"),
            "swell_height_m": wave_current.get("swell_height_m"),
            "swell_direction_deg": wave_current.get("swell_direction_deg"),
            "swell_period_s": wave_current.get("swell_period_s"),
        }

    # 5. Sauvegarder le snapshot
    try:
        save_weather_snapshot(lat, lon, wind_current, waves_full, currents)
    except Exception as e:  # noqa: BLE001
        logger.error("Échec sauvegarde snapshot : %s", e)

    # 6. Sauvegarder les prévisions
    collected_at = datetime.now(timezone.utc).isoformat()

    if wind_forecasts:
        try:
            save_forecasts(collected_at, "wind", wind_forecasts)
        except Exception as e:  # noqa: BLE001
            logger.error("Échec sauvegarde prévisions vent : %s", e)

    if wave_forecasts:
        try:
            save_forecasts(collected_at, "wave", wave_forecasts)
        except Exception as e:  # noqa: BLE001
            logger.error("Échec sauvegarde prévisions vagues : %s", e)

    logger.info("=== Collecte météo/océan terminée ===")


if __name__ == "__main__":
    main()
