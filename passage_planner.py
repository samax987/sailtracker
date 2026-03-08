#!/usr/bin/env python3
"""
passage_planner.py — Passage planner multi-modèle avec ensembles.
Tourne en cron toutes les 6 heures, ou lancé via --route-id pour une route spécifique.
"""

import argparse
import json
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys

import numpy as np
import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
try:
    from polars import get_polar, PolarDiagram
    _HAS_POLARS = True
except ImportError:
    _HAS_POLARS = False

# =============================================================================
# Configuration
# =============================================================================

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = BASE_DIR / "sailtracker.db"
REQUEST_TIMEOUT = 60
API_DELAY = 0.5

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SERVER_URL = os.getenv("SERVER_URL", "http://localhost:8085")

WIND_MODELS = ["ecmwf_ifs025", "gfs_global", "icon_global", "ecmwf_aifs025"]

VERIF_ZONES = {
    'cabo_verde':     (16.9, -25.0),
    'mid_atlantic':   (15.0, -40.0),
    'caribbean_east': (13.5, -55.0),
    'caribbean_west': (17.9, -62.8),
}

ROUTE_CAPVERT_BARBADE = {
    "name": "Cap-Vert - Barbade",
    "waypoints": [
        {"lat": 16.88, "lon": -25.00, "name": "Mindelo, Cap-Vert"},
        {"lat": 16.50, "lon": -28.00, "name": "WP1 - Large Cap-Vert"},
        {"lat": 15.50, "lon": -33.00, "name": "WP2 - Alizés"},
        {"lat": 14.50, "lon": -38.00, "name": "WP3 - Mi-chemin Est"},
        {"lat": 14.00, "lon": -43.00, "name": "WP4 - Centre Atlantique"},
        {"lat": 13.50, "lon": -48.00, "name": "WP5 - Mi-chemin Ouest"},
        {"lat": 13.20, "lon": -53.00, "name": "WP6 - Approche"},
        {"lat": 13.10, "lon": -56.00, "name": "WP7 - Large Barbade"},
        {"lat": 13.07, "lon": -59.62, "name": "Bridgetown, Barbade"},
    ],
    "boat_speed_avg_knots": 6.0,
    "max_wind_knots": 30,
    "max_wave_m": 3.0,
    "max_swell_m": 3.5,
}

# =============================================================================
# Logging
# =============================================================================

log_dir = BASE_DIR / "logs"
log_dir.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("passage_planner")


# =============================================================================
# Utilitaires géographiques
# =============================================================================

def haversine_nm(lat1, lon1, lat2, lon2):
    R = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def calculate_route_distance(waypoints):
    total = 0.0
    for i in range(1, len(waypoints)):
        total += haversine_nm(
            waypoints[i - 1]["lat"], waypoints[i - 1]["lon"],
            waypoints[i]["lat"], waypoints[i]["lon"],
        )
    return total


def interpolate_position(waypoints, hours_elapsed, speed_knots):
    distance_covered = hours_elapsed * speed_knots
    cumulative = 0.0
    for i in range(1, len(waypoints)):
        seg_dist = haversine_nm(
            waypoints[i - 1]["lat"], waypoints[i - 1]["lon"],
            waypoints[i]["lat"], waypoints[i]["lon"],
        )
        if cumulative + seg_dist >= distance_covered:
            frac = (distance_covered - cumulative) / seg_dist
            lat = waypoints[i - 1]["lat"] + frac * (waypoints[i]["lat"] - waypoints[i - 1]["lat"])
            lon = waypoints[i - 1]["lon"] + frac * (waypoints[i]["lon"] - waypoints[i - 1]["lon"])
            return {"lat": lat, "lon": lon, "wp_index": i - 1}
        cumulative += seg_dist
    last = waypoints[-1]
    return {"lat": last["lat"], "lon": last["lon"], "wp_index": len(waypoints) - 1}


def bearing(lat1, lon1, lat2, lon2):
    dlon = math.radians(lon2 - lon1)
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


# =============================================================================
# Collecte Open-Meteo
# =============================================================================

def fetch_wind_model(lat, lon, model):
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
        "wind_speed_unit": "kn", "models": model, "forecast_days": 16,
    }
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("Erreur fetch wind %s @ (%.2f,%.2f) : %s", model, lat, lon, e)
        return None


def fetch_marine(lat, lon):
    url = "https://marine-api.open-meteo.com/v1/marine"
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "wave_height,wave_direction,wave_period,swell_wave_height,swell_wave_direction,swell_wave_period,ocean_current_velocity,ocean_current_direction",
        "forecast_days": 8,
    }
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("Erreur fetch marine @ (%.2f,%.2f) : %s", lat, lon, e)
        return None


def fetch_ensemble(lat, lon):
    url = "https://ensemble-api.open-meteo.com/v1/ensemble"
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "kn", "models": "ecmwf_ifs025", "forecast_days": 16,
    }
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("Erreur fetch ensemble @ (%.2f,%.2f) : %s", lat, lon, e)
        return None


def parse_ensemble_stats(data):
    if not data or "hourly" not in data:
        return {}
    hourly = data["hourly"]
    times = hourly.get("time", [])
    members_data = []
    for key, vals in hourly.items():
        if "wind_speed_10m_member" in key and isinstance(vals, list):
            members_data.append(vals)
    if not members_data or not times:
        return {}
    members_arr = np.array(members_data, dtype=float)
    result = {}
    for i, t in enumerate(times):
        if i % 6 != 0:
            continue
        col = members_arr[:, i]
        valid = col[~np.isnan(col)]
        if len(valid) == 0:
            continue
        result[t] = {
            "mean": float(np.mean(valid)), "std": float(np.std(valid)),
            "min": float(np.min(valid)), "max": float(np.max(valid)),
            "n_members": int(len(valid)),
        }
    return result


def store_ensemble_members(ensemble_data, route_id, wp_idx, collected_at, conn):
    """Stocke les 51 membres ensembles individuels en base de données."""
    if not ensemble_data or "hourly" not in ensemble_data:
        return 0
    hourly = ensemble_data["hourly"]
    times = hourly.get("time", [])
    if not times:
        return 0

    # Détecter les membres disponibles
    member_speeds = {}
    member_dirs = {}
    for key, vals in hourly.items():
        if "wind_speed_10m_member" in key and isinstance(vals, list):
            try:
                mid = int(key.split("member")[1])
                member_speeds[mid] = vals
            except ValueError:
                pass
        elif "wind_direction_10m_member" in key and isinstance(vals, list):
            try:
                mid = int(key.split("member")[1])
                member_dirs[mid] = vals
            except ValueError:
                pass

    if not member_speeds:
        logger.warning("Aucun membre ensemble trouvé dans les données")
        return 0

    rows = []
    # Sous-échantillonner toutes les 6h pour limiter le volume
    for t_idx, t in enumerate(times):
        if t_idx % 6 != 0:
            continue
        for mid in sorted(member_speeds.keys()):
            speeds = member_speeds[mid]
            dirs = member_dirs.get(mid, [])
            ws = speeds[t_idx] if t_idx < len(speeds) else None
            wd = dirs[t_idx] if t_idx < len(dirs) else None
            rows.append((
                collected_at, route_id, wp_idx, t, "ecmwf_ens", mid,
                ws, wd,
            ))

    if rows:
        conn.executemany(
            """INSERT INTO ensemble_forecasts
               (collected_at, route_id, waypoint_index, forecast_time, model, member_id,
                wind_speed_knots, wind_direction_deg)
               VALUES (?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.commit()
        logger.info("WP%d : %d entrées ensemble stockées (%d membres)", wp_idx, len(rows), len(member_speeds))
    return len(rows)


def get_model_weights(conn, zone_lat, zone_lon):
    """Retourne les poids basés sur l'erreur historique 30j."""
    def haversine_deg(z1, z2):
        return abs(z1[0] - z2[0]) + abs(z1[1] - z2[1])

    nearest_zone = min(VERIF_ZONES.items(), key=lambda z: haversine_deg(z[1], (zone_lat, zone_lon)))
    zone_name = nearest_zone[0]

    try:
        rows = conn.execute(
            """SELECT model, AVG(wind_speed_error_avg) as avg_err, COUNT(*) as n
               FROM model_accuracy
               WHERE zone=? AND forecast_hour <= 72 AND date >= date('now','-30 days')
               GROUP BY model HAVING n >= 5""",
            (zone_name,)
        ).fetchall()
    except Exception:
        return None

    if not rows:
        return None

    weights = {r["model"]: 1.0 / (r["avg_err"] + 1.0) for r in rows}
    total = sum(weights.values())
    n = len(weights)
    return {k: v * n / total for k, v in weights.items()}


# =============================================================================
# Scores
# =============================================================================

def calculate_confidence_score(models_wind_speeds, ensemble_std, forecast_hour):
    model_std = float(np.std(models_wind_speeds)) if len(models_wind_speeds) > 1 else 0.0
    if model_std < 3: score_models = 100
    elif model_std < 8: score_models = 70
    elif model_std < 15: score_models = 40
    else: score_models = 10

    if ensemble_std < 2: score_ensemble = 100
    elif ensemble_std < 5: score_ensemble = 75
    elif ensemble_std < 10: score_ensemble = 40
    else: score_ensemble = 10

    days_ahead = forecast_hour / 24
    if days_ahead <= 2: score_horizon = 100
    elif days_ahead <= 5: score_horizon = 75
    elif days_ahead <= 8: score_horizon = 50
    elif days_ahead <= 12: score_horizon = 25
    else: score_horizon = 10

    return 0.4 * score_models + 0.4 * score_ensemble + 0.2 * score_horizon


def calculate_comfort_score(wind_knots, wind_dir, wave_m, wave_dir, wave_period,
                             swell_m, swell_dir, boat_heading, boat_limits,
                             current_speed_kn=None, current_dir_deg=None):
    score = 100.0
    if wind_knots is None:
        return 50.0
    max_wind = boat_limits.get("max_wind_knots", 30)
    if wind_knots > max_wind:
        return 0.0
    if wind_knots > 20: score -= (wind_knots - 20) * 5
    elif wind_knots < 10: score -= (10 - wind_knots) * 3
    if wind_dir is not None:
        angle_rel = abs(wind_dir - boat_heading) % 360
        if angle_rel > 180: angle_rel = 360 - angle_rel
        if angle_rel < 45: score -= 30
        elif angle_rel < 90: score -= 10
    max_wave = boat_limits.get("max_wave_m", 3.0)
    if wave_m is not None:
        if wave_m > max_wave: return 0.0
        if wave_m > 2.0: score -= (wave_m - 2.0) * 15
    if wave_period is not None and wave_period < 6:
        score -= (6 - wave_period) * 10
    max_swell = boat_limits.get("max_swell_m", 3.5)
    if swell_m is not None and swell_m > max_swell:
        score = max(0.0, score - 40)
    if wave_dir is not None and swell_dir is not None:
        cross_angle = abs(wave_dir - swell_dir) % 360
        if cross_angle > 180: cross_angle = 360 - cross_angle
        if cross_angle > 60: score -= 20
    if current_speed_kn and current_dir_deg is not None:
        angle_diff = math.radians(current_dir_deg - boat_heading)
        along_track = current_speed_kn * math.cos(angle_diff)
        if along_track >= 0.5:
            score = min(100, score + min(10, along_track * 5))
        elif along_track <= -0.3:
            score -= min(20, abs(along_track) * 10)
    return max(0.0, min(100.0, score))


# =============================================================================
# Collecte + departure planner
# =============================================================================

def collect_forecasts_for_route(route, conn):
    collected_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    route_id = route["id"]
    waypoints = route["waypoints"]

    for wp_idx, wp in enumerate(waypoints):
        lat, lon = wp["lat"], wp["lon"]
        logger.info("WP%d/%d : %s (%.3f, %.3f)", wp_idx + 1, len(waypoints), wp.get("name", ""), lat, lon)

        wind_by_model = {}
        for model in WIND_MODELS:
            data = fetch_wind_model(lat, lon, model)
            if data and "hourly" in data:
                wind_by_model[model] = data["hourly"]
            time.sleep(API_DELAY)

        marine_data = fetch_marine(lat, lon)
        marine = marine_data.get("hourly", {}) if marine_data else {}
        time.sleep(API_DELAY)

        ensemble_data = fetch_ensemble(lat, lon)
        ensemble_stats = parse_ensemble_stats(ensemble_data)
        store_ensemble_members(ensemble_data, route_id, wp_idx, collected_at, conn)
        time.sleep(API_DELAY)

        times = []
        for model in WIND_MODELS:
            if model in wind_by_model and "time" in wind_by_model[model]:
                times = wind_by_model[model]["time"]
                break

        if not times:
            logger.warning("Pas de données temporelles pour WP%d", wp_idx)
            continue

        rows = []
        for t_idx, t in enumerate(times):
            wind_speeds = []
            wind_dir = None
            wind_gusts = None
            first_model = True
            for model in WIND_MODELS:
                mdata = wind_by_model.get(model, {})
                speeds_list = mdata.get("wind_speed_10m", [])
                dirs_list = mdata.get("wind_direction_10m", [])
                gusts_list = mdata.get("wind_gusts_10m", [])
                ws = speeds_list[t_idx] if t_idx < len(speeds_list) else None
                if ws is not None:
                    wind_speeds.append(ws)
                if first_model and ws is not None:
                    wind_dir = dirs_list[t_idx] if t_idx < len(dirs_list) else None
                    wind_gusts = gusts_list[t_idx] if t_idx < len(gusts_list) else None
                    first_model = False

            m_times = marine.get("time", [])
            m_idx = m_times.index(t) if t in m_times else None
            wave_h = marine.get("wave_height", [])[m_idx] if m_idx is not None and "wave_height" in marine else None
            wave_dir = marine.get("wave_direction", [])[m_idx] if m_idx is not None else None
            wave_per = marine.get("wave_period", [])[m_idx] if m_idx is not None else None
            swell_h = marine.get("swell_wave_height", [])[m_idx] if m_idx is not None else None
            swell_dir = marine.get("swell_wave_direction", [])[m_idx] if m_idx is not None else None
            swell_per = marine.get("swell_wave_period", [])[m_idx] if m_idx is not None else None
            curr_vel = marine.get("ocean_current_velocity", [])[m_idx] if m_idx is not None and "ocean_current_velocity" in marine else None
            curr_dir = marine.get("ocean_current_direction", [])[m_idx] if m_idx is not None and "ocean_current_direction" in marine else None
            curr_kn = curr_vel / 1.852 if curr_vel is not None else None
            avg_wind = float(np.mean(wind_speeds)) if wind_speeds else None

            rows.append((
                route_id, collected_at, wp_idx, lat, lon, t, "multi",
                avg_wind, wind_dir, wind_gusts,
                wave_h, wave_dir, wave_per,
                swell_h, swell_dir, swell_per,
                curr_kn, curr_dir,
            ))

        conn.executemany(
            """INSERT INTO passage_forecasts
               (route_id,collected_at,waypoint_index,latitude,longitude,forecast_time,model,
                wind_speed_knots,wind_direction_deg,wind_gusts_knots,
                wave_height_m,wave_direction_deg,wave_period_s,
                swell_height_m,swell_direction_deg,swell_period_s,
                current_speed_knots,current_direction_deg)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.commit()
        logger.info("WP%d : %d prévisions stockées", wp_idx, len(rows))

    return collected_at


def get_forecast_at(forecasts_by_wp, wp_idx, target_time):
    wp_forecasts = forecasts_by_wp.get(wp_idx, [])
    if not wp_forecasts:
        return None
    target_str = target_time.strftime("%Y-%m-%dT%H:00")
    for fc in wp_forecasts:
        if fc["forecast_time"].startswith(target_str[:13]):
            return fc
    best = None
    best_diff = float("inf")
    for fc in wp_forecasts:
        try:
            fc_dt = datetime.fromisoformat(fc["forecast_time"].replace("Z", ""))
        except ValueError:
            continue
        diff = abs((fc_dt - target_time).total_seconds())
        if diff < best_diff:
            best_diff = diff
            best = fc
    return best if best_diff < 7 * 3600 else None


def _twa_from_hdg_twd(hdg: float, twd: float) -> float:
    """Angle au vent vrai (0-180°)."""
    angle = (twd - hdg + 360) % 360
    return angle if angle <= 180 else 360 - angle


def simulate_departure(route, departure_dt, forecasts_by_wp, polar=None):
    """
    Simule un départ pas-à-pas (pas de 3h), avec vitesse calculée via les
    polaires (TWA/TWS → kts) si polar est fourni, sinon vitesse fixe.

    Retourne :
        departure_date, confidence_score, comfort_score, overall_score, alerts,
        current_effect_knots, adjusted_eta_hours, avg_polar_speed_kts, used_polars
    """
    waypoints = route["waypoints"]
    speed_fallback = route["boat_speed_avg_knots"]
    total_dist = calculate_route_distance(waypoints)
    avg_bearing = bearing(
        waypoints[0]["lat"], waypoints[0]["lon"],
        waypoints[-1]["lat"], waypoints[-1]["lon"],
    )

    STEP_H = 3
    # Limite de sécurité : 3× le temps estimé à vitesse min, au moins 10j
    max_steps = max(80, int(total_dist / max(1.0, speed_fallback) / STEP_H * 3))

    # État courant
    cur_lat = waypoints[0]["lat"]
    cur_lon = waypoints[0]["lon"]
    cur_wp_idx = 0
    elapsed_h = 0.0

    hourly_confidence = []
    hourly_comfort = []
    current_effects = []
    alerts = []
    polar_speeds = []

    for _step in range(max_steps):
        if cur_wp_idx >= len(waypoints) - 1:
            break

        current_time = departure_dt + timedelta(hours=elapsed_h)
        next_wp = waypoints[cur_wp_idx + 1]
        hdg = bearing(cur_lat, cur_lon, next_wp["lat"], next_wp["lon"])

        fc = get_forecast_at(forecasts_by_wp, cur_wp_idx, current_time)

        # ── Vitesse via polaires ──────────────────────────────────────────────
        boat_speed = speed_fallback
        used_polar_this_step = False
        if polar is not None and fc and fc.get("wind_speed_knots") and fc.get("wind_direction_deg") is not None:
            tws = fc["wind_speed_knots"]
            twd = fc["wind_direction_deg"]
            twa = _twa_from_hdg_twd(hdg, twd)
            spd = polar.get_boat_speed(twa, tws)
            if spd >= 1.0:
                boat_speed = spd
                used_polar_this_step = True

        polar_speeds.append(boat_speed)

        # ── Scores ──────────────────────────────────────────────────────────
        if fc:
            wind_speeds_list = [fc["wind_speed_knots"]] if fc.get("wind_speed_knots") else []
            conf = calculate_confidence_score(wind_speeds_list, ensemble_std=3.0, forecast_hour=elapsed_h)
            curr_spd = fc.get("current_speed_knots")
            curr_dir_fc = fc.get("current_direction_deg")
            comf = calculate_comfort_score(
                wind_knots=fc.get("wind_speed_knots"), wind_dir=fc.get("wind_direction_deg"),
                wave_m=fc.get("wave_height_m"), wave_dir=fc.get("wave_direction_deg"),
                wave_period=fc.get("wave_period_s"), swell_m=fc.get("swell_height_m"),
                swell_dir=fc.get("swell_direction_deg"), boat_heading=hdg, boat_limits=route,
                current_speed_kn=curr_spd, current_dir_deg=curr_dir_fc,
            )
            if curr_spd and curr_dir_fc is not None:
                along = curr_spd * math.cos(math.radians(curr_dir_fc - hdg))
                current_effects.append(along)
            hourly_confidence.append(conf)
            hourly_comfort.append(comf)

            day = int(elapsed_h) // 24
            wind_val = fc.get("wind_speed_knots")
            wave_val = fc.get("wave_height_m")
            if wind_val and wind_val > route["max_wind_knots"]:
                alert = f"J+{day} : Vent {wind_val:.0f} nds dépasse limite {route['max_wind_knots']:.0f} nds"
                if alert not in alerts:
                    alerts.append(alert)
            if wave_val and wave_val > route["max_wave_m"]:
                alert = f"J+{day} : Vagues {wave_val:.1f}m dépassent limite {route['max_wave_m']:.1f}m"
                if alert not in alerts:
                    alerts.append(alert)

        # ── Déplacement ──────────────────────────────────────────────────────
        dist_moved = boat_speed * STEP_H
        # Correction courant le long du cap
        if fc and fc.get("current_speed_knots") and fc.get("current_direction_deg") is not None:
            along_curr = fc["current_speed_knots"] * math.cos(
                math.radians(fc["current_direction_deg"] - hdg)
            )
            dist_moved += along_curr * STEP_H

        dist_moved = max(0.0, dist_moved)
        dist_to_next = haversine_nm(cur_lat, cur_lon, next_wp["lat"], next_wp["lon"])

        if dist_moved >= dist_to_next:
            cur_lat = next_wp["lat"]
            cur_lon = next_wp["lon"]
            cur_wp_idx += 1
        else:
            frac = dist_moved / dist_to_next
            cur_lat += frac * (next_wp["lat"] - cur_lat)
            cur_lon += frac * (next_wp["lon"] - cur_lon)

        elapsed_h += STEP_H

    if not hourly_confidence:
        return {
            "departure_date": departure_dt.isoformat(),
            "confidence_score": 0.0, "comfort_score": 0.0,
            "overall_score": 0.0, "alerts": ["Données insuffisantes"],
        }

    avg_conf = float(np.mean(hourly_confidence))
    avg_comf = float(np.mean(hourly_comfort))
    min_comf = float(np.min(hourly_comfort))
    overall = 0.3 * avg_conf + 0.4 * avg_comf + 0.3 * min_comf
    avg_current = float(np.mean(current_effects)) if current_effects else 0.0
    avg_polar_speed = float(np.mean(polar_speeds)) if polar_speeds else speed_fallback

    return {
        "departure_date": departure_dt.isoformat(),
        "confidence_score": round(avg_conf, 1),
        "comfort_score": round(avg_comf, 1),
        "overall_score": round(overall, 1),
        "alerts": alerts[:10],
        "current_effect_knots": round(avg_current, 2),
        "adjusted_eta_hours": round(elapsed_h, 1),
        "avg_polar_speed_kts": round(avg_polar_speed, 2),
        "used_polars": polar is not None,
    }


# =============================================================================
# Telegram
# =============================================================================

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning("Erreur Telegram : %s", e)
        return False


def build_telegram_message(route, best_sim):
    dep_dt = datetime.fromisoformat(best_sim["departure_date"])
    dep_str = dep_dt.strftime("%A %d %B %Y")
    alerts_txt = "\n".join(f"⚠️ {a}" for a in best_sim["alerts"]) if best_sim["alerts"] else "✅ Aucune alerte critique"
    return (
        f"<b>⛵ FENÊTRE DE DÉPART DÉTECTÉE !</b>\n\n"
        f"<b>Route :</b> {route['name']}\n"
        f"<b>Départ optimal :</b> {dep_str}\n\n"
        f"🎯 Confiance : <b>{best_sim['confidence_score']:.0f}/100</b>\n"
        f"🛥 Confort : <b>{best_sim['comfort_score']:.0f}/100</b>\n"
        f"⭐ Score global : <b>{best_sim['overall_score']:.0f}/100</b>\n\n"
        f"<b>Alertes :</b>\n{alerts_txt}\n\n"
        f"<a href='{SERVER_URL}/passage'>📊 Voir le Passage Planner</a>"
    )


# =============================================================================
# Helpers DB
# =============================================================================

def ensure_route(conn, route_def):
    row = conn.execute("SELECT id FROM passage_routes WHERE name = ?", (route_def["name"],)).fetchone()
    if row:
        return row[0]
    wps_json = json.dumps(route_def["waypoints"], ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO passage_routes (name, waypoints, boat_speed_avg_knots, max_wind_knots, max_wave_m, max_swell_m, status) VALUES (?, ?, ?, ?, ?, ?, 'ready')",
        (route_def["name"], wps_json, route_def["boat_speed_avg_knots"],
         route_def["max_wind_knots"], route_def["max_wave_m"], route_def["max_swell_m"]),
    )
    conn.commit()
    return cur.lastrowid


def load_forecasts_by_wp(conn, route_id, collected_at):
    rows = conn.execute(
        """SELECT waypoint_index, forecast_time,
               wind_speed_knots, wind_direction_deg, wind_gusts_knots,
               wave_height_m, wave_direction_deg, wave_period_s,
               swell_height_m, swell_direction_deg, swell_period_s,
               current_speed_knots, current_direction_deg
           FROM passage_forecasts WHERE route_id = ? AND collected_at = ?
           ORDER BY waypoint_index, forecast_time""",
        (route_id, collected_at),
    ).fetchall()
    result = {}
    for row in rows:
        wp_idx = row[0]
        if wp_idx not in result:
            result[wp_idx] = []
        result[wp_idx].append({
            "forecast_time": row[1], "wind_speed_knots": row[2],
            "wind_direction_deg": row[3], "wind_gusts_knots": row[4],
            "wave_height_m": row[5], "wave_direction_deg": row[6], "wave_period_s": row[7],
            "swell_height_m": row[8], "swell_direction_deg": row[9], "swell_period_s": row[10],
            "current_speed_knots": row[11], "current_direction_deg": row[12],
        })
    return result


def set_route_status(conn, route_id, status, last_computed=None):
    try:
        if last_computed:
            conn.execute("UPDATE passage_routes SET status=?, last_computed=? WHERE id=?",
                         (status, last_computed, route_id))
        else:
            conn.execute("UPDATE passage_routes SET status=? WHERE id=?", (status, route_id))
        conn.commit()
    except Exception as e:
        logger.warning("Impossible de mettre à jour le statut : %s", e)


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="SailTracker Passage Planner")
    parser.add_argument("--route-id", type=int, default=None, dest="route_id",
                        help="ID de la route à calculer (défaut: Cap-Vert → Barbade)")
    args = parser.parse_args()

    logger.info("=== Passage Planner démarré (route_id=%s) ===", args.route_id or "défaut")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if args.route_id:
        row = conn.execute("SELECT * FROM passage_routes WHERE id = ?", (args.route_id,)).fetchone()
        if not row:
            logger.error("Route ID=%d non trouvée en base", args.route_id)
            conn.close()
            return
        route = {
            "id": row["id"],
            "name": row["name"],
            "waypoints": json.loads(row["waypoints"]),
            "boat_speed_avg_knots": row["boat_speed_avg_knots"],
            "max_wind_knots": row["max_wind_knots"],
            "max_wave_m": row["max_wave_m"],
            "max_swell_m": row["max_swell_m"],
        }
        logger.info("Route chargée depuis DB : '%s'", route["name"])
    else:
        route_id = ensure_route(conn, ROUTE_CAPVERT_BARBADE)
        route = dict(ROUTE_CAPVERT_BARBADE)
        route["id"] = route_id

    set_route_status(conn, route["id"], "computing")

    # Chargement du diagramme polaire (si disponible)
    polar = None
    if _HAS_POLARS:
        try:
            polar = get_polar()
            logger.info("Polaires POLLEN 1 chargées — simulation dynamique activée")
        except Exception as e:
            logger.warning("Impossible de charger les polaires : %s — vitesse fixe utilisée", e)

    try:
        logger.info("Collecte multi-modèles pour %d waypoints...", len(route["waypoints"]))
        collected_at = collect_forecasts_for_route(route, conn)
        logger.info("Collecte terminée à %s", collected_at)

        forecasts_by_wp = load_forecasts_by_wp(conn, route["id"], collected_at)

        logger.info("Calcul du departure planner sur 15 jours (polaires=%s)...", polar is not None)
        now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        now_naive = now.replace(tzinfo=None)
        computed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        simulations = []

        for day_offset in range(15):
            dep_dt = now_naive + timedelta(days=day_offset)
            sim = simulate_departure(route, dep_dt, forecasts_by_wp, polar=polar)
            simulations.append(sim)
            sim_summary = json.dumps({
                "current_effect_knots": sim.get("current_effect_knots", 0.0),
                "adjusted_eta_hours": sim.get("adjusted_eta_hours"),
                "avg_polar_speed_kts": sim.get("avg_polar_speed_kts"),
                "used_polars": sim.get("used_polars", False),
            }, ensure_ascii=False)
            conn.execute(
                "INSERT INTO departure_simulations (route_id,computed_at,departure_date,confidence_score,comfort_score,overall_score,summary,alerts) VALUES (?,?,?,?,?,?,?,?)",
                (route["id"], computed_at, sim["departure_date"], sim["confidence_score"],
                 sim["comfort_score"], sim["overall_score"], sim_summary,
                 json.dumps(sim["alerts"], ensure_ascii=False)),
            )
            polar_info = f" spd={sim.get('avg_polar_speed_kts','?')}kts" if sim.get("used_polars") else ""
            logger.info("J+%02d (%s) : conf=%.0f comf=%.0f overall=%.0f eta=%.0fh%s",
                        day_offset, dep_dt.strftime("%d/%m"),
                        sim["confidence_score"], sim["comfort_score"], sim["overall_score"],
                        sim.get("adjusted_eta_hours", 0), polar_info)

        conn.commit()

        # Nettoyage
        conn.execute("DELETE FROM departure_simulations WHERE computed_at < datetime('now', '-30 days')")
        conn.execute("DELETE FROM passage_forecasts WHERE collected_at < datetime('now', '-7 days')")
        conn.execute("DELETE FROM ensemble_forecasts WHERE collected_at < datetime('now', '-3 days')")
        conn.commit()

        last_computed = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        set_route_status(conn, route["id"], "ready", last_computed)

        best = max(simulations, key=lambda s: s["overall_score"])
        logger.info("Meilleure fenêtre : %s (score=%.0f)", best["departure_date"][:10], best["overall_score"])

        if best["overall_score"] >= 70:
            msg = build_telegram_message(route, best)
            if send_telegram(msg):
                logger.info("Alerte Telegram envoyée !")

    except Exception as e:
        logger.error("Erreur lors du calcul : %s", e, exc_info=True)
        set_route_status(conn, route["id"], "error")
    finally:
        conn.close()

    logger.info("=== Passage Planner terminé ===")


if __name__ == "__main__":
    main()
