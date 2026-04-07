#!/usr/bin/env python3
"""
wind_alert_monitor.py — Surveillance vent en temps réel avec alertes Telegram.

Logique :
  1. Récupère la dernière position du bateau depuis la DB
  2. Appelle Open-Meteo pour le vent actuel + prévision 3h
  3. Vérifie les conditions d'alerte (franchissement de seuil, tendance, prévision, critique)
  4. Envoie une alerte Telegram si non-spam (max 1 alerte / type / 2h)

Cron : */15 * * * *
"""

import json
import logging
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

sys.path.insert(0, str(BASE_DIR))
from telegram_utils import send_telegram

DB_PATH = BASE_DIR / "sailtracker.db"
LOG_PATH = BASE_DIR / "logs" / "wind_alerts.log"
STATE_FILE = Path("/tmp/sailtracker_wind_alerts.json")

ANTI_SPAM_SECONDS = 2 * 3600  # 2 heures
FORECAST_HOURS = 3
REQUEST_TIMEOUT = 15
DASHBOARD_URL = "http://45.55.239.73:8085/quart"
BOAT_NAME = "POLLEN 1"

# Seuils de bandes (nœuds)
WIND_BANDS = [8, 15, 20, 25, 30, 38]

# ── Logging ──────────────────────────────────────────────────────────────────

LOG_PATH.parent.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("wind_alert")

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "SailTracker/1.0 (wind-alert)"})


# ── Recommandation voile ──────────────────────────────────────────────────────

def get_sail_recommendation(tws_kts: float) -> str:
    if tws_kts < 8:
        return "Plein voile"
    elif tws_kts < 15:
        return "Plein voile"
    elif tws_kts < 20:
        return "1 ris"
    elif tws_kts < 25:
        return "2 ris + Génois 80%"
    elif tws_kts < 30:
        return "3 ris + Génois 50%"
    elif tws_kts < 38:
        return "4 ris + Génois 30%"
    else:
        return "4 ris — Tempête!"


# ── Bande de vent ─────────────────────────────────────────────────────────────

def get_band(tws_kts: float) -> int:
    """Retourne le numéro de bande (0 = < 8 kts, 1 = 8-15, etc.)"""
    for i, threshold in enumerate(WIND_BANDS):
        if tws_kts < threshold:
            return i
    return len(WIND_BANDS)


# ── Direction en texte ────────────────────────────────────────────────────────

def dir_to_compass(deg: float) -> str:
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSO", "SO", "OSO", "O", "ONO", "NO", "NNO"]
    idx = round(deg / 22.5) % 16
    return dirs[idx]


# ── DB ────────────────────────────────────────────────────────────────────────

def get_last_position() -> dict | None:
    """Récupère la dernière position connue du bateau."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row

        # Essai user_id=1 d'abord, puis toute position récente
        row = conn.execute("""
            SELECT latitude, longitude, timestamp
            FROM positions
            WHERE user_id = 1
            ORDER BY timestamp DESC
            LIMIT 1
        """).fetchone()

        if row is None:
            row = conn.execute("""
                SELECT latitude, longitude, timestamp
                FROM positions
                ORDER BY timestamp DESC
                LIMIT 1
            """).fetchone()

        conn.close()
        if row:
            return dict(row)
    except Exception as e:
        logger.error("Erreur lecture DB positions: %s", e)
    return None


# ── Open-Meteo ────────────────────────────────────────────────────────────────

def fetch_wind_forecast(lat: float, lon: float) -> dict | None:
    """
    Récupère le vent actuel + prévision 3h depuis Open-Meteo.
    Retourne un dict avec current et forecast_max.
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": round(lat, 4),
        "longitude": round(lon, 4),
        "hourly": "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "kn",
        "models": "ecmwf_ifs025",
        "forecast_days": 1,
        "past_hours": 1,
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
            return None

        # Heure courante UTC
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        # Trouver l'index actuel (heure la plus proche de maintenant)
        best_idx = 0
        best_diff = float("inf")
        for i, t_str in enumerate(times):
            try:
                t_dt = datetime.fromisoformat(t_str)
                diff = abs((t_dt - now).total_seconds())
                if diff < best_diff:
                    best_diff = diff
                    best_idx = i
            except Exception:
                continue

        current_speed = speeds[best_idx] if best_idx < len(speeds) else None
        current_dir = dirs[best_idx] if best_idx < len(dirs) else None

        if current_speed is None:
            return None

        # Prévision sur les FORECAST_HOURS prochaines heures
        forecast_speeds = []
        for i in range(best_idx + 1, min(best_idx + FORECAST_HOURS + 1, len(times))):
            try:
                t_dt = datetime.fromisoformat(times[i])
                if (t_dt - now).total_seconds() <= FORECAST_HOURS * 3600:
                    if speeds[i] is not None:
                        forecast_speeds.append(speeds[i])
            except Exception:
                continue

        forecast_max = max(forecast_speeds) if forecast_speeds else current_speed

        return {
            "tws": float(current_speed),
            "twd": float(current_dir) if current_dir is not None else 0.0,
            "forecast_max_3h": float(forecast_max),
        }

    except Exception as e:
        logger.error("Erreur Open-Meteo: %s", e)
        return None


# ── État anti-spam ────────────────────────────────────────────────────────────

def load_state() -> dict:
    """Charge l'état depuis le fichier JSON."""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {}


def save_state(state: dict):
    """Sauvegarde l'état dans le fichier JSON."""
    try:
        STATE_FILE.write_text(json.dumps(state))
    except Exception as e:
        logger.warning("Erreur sauvegarde état: %s", e)


def can_send_alert(state: dict, alert_type: str) -> bool:
    """Vérifie si on peut envoyer une alerte (anti-spam 2h)."""
    last_sent = state.get(alert_type, 0)
    return (time.time() - last_sent) >= ANTI_SPAM_SECONDS


def mark_alert_sent(state: dict, alert_type: str):
    """Marque une alerte comme envoyée."""
    state[alert_type] = time.time()


# ── Message Telegram ──────────────────────────────────────────────────────────

def build_message(lat: float, lon: float, tws: float, twd: float,
                  trend: float | None, forecast_max: float,
                  alert_types: list[str]) -> str:
    lat_str = f"{abs(lat):.1f}°{'N' if lat >= 0 else 'S'}"
    lon_str = f"{abs(lon):.1f}°{'E' if lon >= 0 else 'W'}"
    compass = dir_to_compass(twd)
    reco = get_sail_recommendation(tws)

    lines = [
        f"⚠️ <b>Alerte vent — {BOAT_NAME}</b>",
        f"📍 {lat_str} {lon_str}",
        "",
        f"💨 Vent actuel : <b>{tws:.1f} kts</b> ({compass} {twd:.0f}°)",
    ]

    if trend is not None and abs(trend) >= 0.5:
        sign = "+" if trend >= 0 else ""
        lines.append(f"📈 Tendance : {sign}{trend:.1f} kts / 15 min")

    lines.append(f"🔮 Prévi 3h : jusqu'à {forecast_max:.0f} kts")
    lines.append("")
    lines.append(f"👉 <b>{reco} conseillé(s)</b>")
    lines.append("")
    lines.append(f'<a href="{DASHBOARD_URL}">Ouvrir le tableau de bord</a>')

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    logger.info("=== Vérification vent ===")

    # 1. Dernière position
    pos = get_last_position()
    if pos is None:
        logger.warning("Pas de position disponible")
        return

    lat, lon = pos["latitude"], pos["longitude"]
    logger.info("Position : %.4f, %.4f (ts: %s)", lat, lon, pos["timestamp"])

    # 2. Vent actuel + prévision
    wind = fetch_wind_forecast(lat, lon)
    if wind is None:
        logger.warning("Impossible de récupérer le vent")
        return

    tws = wind["tws"]
    twd = wind["twd"]
    forecast_max = wind["forecast_max_3h"]
    logger.info("TWS=%.1f kts, TWD=%.0f°, Prévision 3h max=%.1f kts", tws, twd, forecast_max)

    # 3. État anti-spam
    state = load_state()
    last_tws = state.get("last_tws")

    # Calcul tendance
    trend = (tws - last_tws) if last_tws is not None else None

    # 4. Vérification des conditions d'alerte
    triggered_alerts = []

    # Alerte franchissement de bande
    if last_tws is not None:
        old_band = get_band(last_tws)
        new_band = get_band(tws)
        if old_band != new_band and can_send_alert(state, "band_change"):
            triggered_alerts.append("band_change")
            logger.info("Franchissement de bande: bande %d → %d (%.1f → %.1f kts)",
                        old_band, new_band, last_tws, tws)

    # Alerte tendance (+5 kts en 15 min)
    if trend is not None and trend >= 5.0 and can_send_alert(state, "trend_up"):
        triggered_alerts.append("trend_up")
        logger.info("Tendance hausse: +%.1f kts depuis dernier check", trend)

    # Alerte prévision 3h
    if forecast_max > tws + 5.0 and can_send_alert(state, "forecast_warn"):
        triggered_alerts.append("forecast_warn")
        logger.info("Prévision 3h: +%.1f kts attendus (%.1f → %.1f kts)",
                    forecast_max - tws, tws, forecast_max)

    # Alerte critique
    if tws >= 30.0 and can_send_alert(state, "critical"):
        triggered_alerts.append("critical")
        logger.info("CRITIQUE: TWS=%.1f kts >= 30 kts", tws)

    # 5. Envoi Telegram si besoin
    if triggered_alerts:
        msg = build_message(lat, lon, tws, twd, trend, forecast_max, triggered_alerts)
        logger.info("Envoi alerte Telegram (%s)", ", ".join(triggered_alerts))
        ok = send_telegram(msg)
        if ok:
            for alert_type in triggered_alerts:
                mark_alert_sent(state, alert_type)
            logger.info("Alerte envoyée avec succès")
        else:
            logger.warning("Échec envoi Telegram")
    else:
        logger.info("Aucune alerte à envoyer (TWS=%.1f kts, bande=%d)", tws, get_band(tws))

    # 6. Mise à jour last_tws
    state["last_tws"] = tws
    save_state(state)
    logger.info("État sauvegardé (last_tws=%.1f kts)", tws)


if __name__ == "__main__":
    main()
