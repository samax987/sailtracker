#!/usr/bin/env python3
"""
daily_briefing.py — Résumé Telegram quotidien à 07:00 UTC.
Mode pré-départ : meilleure fenêtre de départ + conditions.
Mode en mer : position, progression, ETA, météo.
"""

import json
import logging
import math
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = BASE_DIR / "sailtracker.db"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SERVER_URL = os.getenv("SERVER_URL", "http://localhost:8085")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "logs" / "daily_briefing.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("daily_briefing")


def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram non configuré")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error("Erreur Telegram : %s", e)
        return False


def haversine_nm(lat1, lon1, lat2, lon2):
    R = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def minutes_ago(ts_str):
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - ts).total_seconds() / 60)
    except Exception:
        return None


def get_at_sea_status(conn):
    """Détecte si le bateau est en navigation active (même logique que /api/at-sea)."""
    pos = conn.execute(
        "SELECT timestamp, latitude, longitude, speed_knots FROM positions WHERE source='inreach' ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    if not pos:
        return None

    age_min = minutes_ago(pos["timestamp"])
    if age_min is None or age_min > 120:
        return None

    speed = pos["speed_knots"] or 0
    if speed < 1.0:
        return None

    lat, lon = float(pos["latitude"]), float(pos["longitude"])

    routes = conn.execute("SELECT id, name, waypoints FROM passage_routes WHERE status='ready'").fetchall()
    best_route = None
    best_dist = 999.0
    for r in routes:
        try:
            wps = json.loads(r["waypoints"])
            for wp in wps:
                d = haversine_nm(lat, lon, wp["lat"], wp["lon"])
                if d < best_dist:
                    best_dist = d
                    best_route = r
        except Exception:
            pass

    if not best_route or best_dist > 50:
        return None

    wps = json.loads(best_route["waypoints"])
    nearest_idx = min(range(len(wps)), key=lambda i: haversine_nm(lat, lon, wps[i]["lat"], wps[i]["lon"]))
    min_wp_dist = haversine_nm(lat, lon, wps[nearest_idx]["lat"], wps[nearest_idx]["lon"])

    total_dist = sum(haversine_nm(wps[i]["lat"], wps[i]["lon"], wps[i+1]["lat"], wps[i+1]["lon"])
                     for i in range(len(wps)-1))
    dist_covered = sum(haversine_nm(wps[i]["lat"], wps[i]["lon"], wps[i+1]["lat"], wps[i+1]["lon"])
                       for i in range(nearest_idx))
    progress_pct = round(dist_covered / total_dist * 100) if total_dist > 0 else 0
    dist_remaining = sum(haversine_nm(wps[i]["lat"], wps[i]["lon"], wps[i+1]["lat"], wps[i+1]["lon"])
                         for i in range(nearest_idx, len(wps)-1)) + min_wp_dist

    speeds_6h = conn.execute(
        "SELECT speed_knots FROM positions WHERE source='inreach' AND speed_knots > 0 AND timestamp >= datetime('now','-6 hours')"
    ).fetchall()
    avg_speed = (sum(r["speed_knots"] for r in speeds_6h) / len(speeds_6h)) if speeds_6h else speed

    eta_str = None
    if avg_speed > 0:
        eta_dt = datetime.now(timezone.utc) + timedelta(hours=dist_remaining / avg_speed)
        eta_str = eta_dt.strftime("%d/%m %Hh%M UTC")

    return {
        "route_name": best_route["name"],
        "route_id": best_route["id"],
        "progress_pct": progress_pct,
        "dist_remaining_nm": round(dist_remaining, 1),
        "eta": eta_str,
        "avg_speed_knots": round(avg_speed, 1),
        "lat": lat, "lon": lon,
        "age_min": age_min,
    }


def get_departure_summary(conn):
    """Meilleure fenêtre de départ parmi toutes les routes."""
    row = conn.execute(
        """SELECT ds.departure_date, ds.confidence_score, ds.comfort_score, ds.overall_score,
                  ds.alerts, ds.summary, pr.name as route_name
           FROM departure_simulations ds
           JOIN passage_routes pr ON ds.route_id = pr.id
           WHERE ds.computed_at >= datetime('now', '-12 hours')
           ORDER BY ds.overall_score DESC
           LIMIT 1"""
    ).fetchone()
    if not row:
        return None
    alerts = []
    if row["alerts"]:
        try:
            alerts = json.loads(row["alerts"])
        except Exception:
            alerts = [row["alerts"]]
    return dict(row) | {"alerts": alerts}


def get_weather_summary(conn):
    row = conn.execute(
        "SELECT wind_speed_kmh, wind_direction_deg, wind_gusts_kmh, wave_height_m, swell_height_m, collected_at FROM weather_snapshots ORDER BY collected_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    wind_kts = round((row["wind_speed_kmh"] or 0) / 1.852, 1)
    gusts_kts = round((row["wind_gusts_kmh"] or 0) / 1.852, 1)
    return {
        "wind_knots": wind_kts,
        "gusts_knots": gusts_kts,
        "wind_dir": row["wind_direction_deg"],
        "wave_m": row["wave_height_m"],
        "swell_m": row["swell_height_m"],
        "collected_at": row["collected_at"],
    }


def wind_dir_to_cardinal(deg):
    if deg is None:
        return "?"
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSO","SO","OSO","O","ONO","NO","NNO"]
    return dirs[round(deg / 22.5) % 16]


def build_at_sea_message(status, wx):
    wind_str = f"{wx['wind_knots']} kts {wind_dir_to_cardinal(wx['wind_dir'])} (rafales {wx['gusts_knots']} kts)" if wx else "—"
    wave_str = f"{wx['wave_m']:.1f} m" if wx and wx['wave_m'] else "—"
    swell_str = f"{wx['swell_m']:.1f} m" if wx and wx['swell_m'] else "—"
    return (
        f"<b>⛵ TRAVERSÉE EN COURS — Rapport 07h00 UTC</b>\n\n"
        f"<b>Route :</b> {status['route_name']}\n"
        f"<b>Position :</b> {status['lat']:.3f}°N, {status['lon']:.3f}°E (il y a {status['age_min']} min)\n\n"
        f"📍 Progression : <b>{status['progress_pct']}%</b>\n"
        f"📏 Distance restante : <b>{status['dist_remaining_nm']} NM</b>\n"
        f"⏱ ETA : <b>{status['eta'] or '—'}</b>\n"
        f"🚤 Vitesse moy. 6h : <b>{status['avg_speed_knots']} kts</b>\n\n"
        f"<b>🌊 Conditions actuelles :</b>\n"
        f"  💨 Vent : {wind_str}\n"
        f"  🌊 Vagues : {wave_str}\n"
        f"  🌊 Houle : {swell_str}\n\n"
        f"<a href='{SERVER_URL}/passage'>📊 Voir le Passage Planner</a>"
    )


def build_pre_departure_message(departure, wx):
    if not departure:
        wind_str = ""
        if wx:
            wind_str = f"\n\n<b>🌊 Conditions actuelles :</b>\n  💨 {wx['wind_knots']} kts {wind_dir_to_cardinal(wx['wind_dir'])}, vagues {(wx['wave_m'] or 0):.1f} m"
        return (
            f"<b>⛵ Rapport météo quotidien — {datetime.now(timezone.utc).strftime('%d/%m/%Y')}</b>\n\n"
            f"❌ Aucune fenêtre de départ calculée récemment.\n"
            f"Relancez le passage planner via /passage.{wind_str}\n\n"
            f"<a href='{SERVER_URL}/passage'>📊 Voir le Passage Planner</a>"
        )

    dep_dt = datetime.fromisoformat(departure["departure_date"])
    dep_str = dep_dt.strftime("%A %d %B à %Hh UTC")
    alerts_txt = "\n".join(f"  ⚠️ {a}" for a in departure["alerts"]) if departure["alerts"] else "  ✅ Aucune alerte critique"
    score = departure["overall_score"]
    score_emoji = "🟢" if score >= 75 else "🟡" if score >= 55 else "🔴"
    wind_str = f"💨 {wx['wind_knots']} kts {wind_dir_to_cardinal(wx['wind_dir'])}, vagues {(wx['wave_m'] or 0):.1f} m" if wx else "—"
    return (
        f"<b>⛵ Rapport météo quotidien — {datetime.now(timezone.utc).strftime('%d/%m/%Y')}</b>\n\n"
        f"<b>Route :</b> {departure['route_name']}\n"
        f"<b>Meilleure fenêtre :</b> {dep_str}\n\n"
        f"{score_emoji} Score global : <b>{score:.0f}/100</b>\n"
        f"🎯 Confiance : <b>{departure['confidence_score']:.0f}/100</b>\n"
        f"🛥 Confort : <b>{departure['comfort_score']:.0f}/100</b>\n\n"
        f"<b>Alertes :</b>\n{alerts_txt}\n\n"
        f"<b>🌊 Conditions actuelles :</b>\n  {wind_str}\n\n"
        f"<a href='{SERVER_URL}/passage'>📊 Voir le Passage Planner</a>"
    )


def main():
    logger.info("=== Résumé Telegram quotidien ===")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    at_sea = get_at_sea_status(conn)
    wx = get_weather_summary(conn)

    if at_sea:
        logger.info("Mode EN MER détecté — route %s, %d%% fait", at_sea["route_name"], at_sea["progress_pct"])
        msg = build_at_sea_message(at_sea, wx)
    else:
        departure = get_departure_summary(conn)
        logger.info("Mode PRÉ-DÉPART — meilleure fenêtre : %s", departure["departure_date"] if departure else "aucune")
        msg = build_pre_departure_message(departure, wx)

    conn.close()

    if send_telegram(msg):
        logger.info("Résumé Telegram envoyé")
    else:
        logger.error("Échec envoi Telegram")


if __name__ == "__main__":
    main()
