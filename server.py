#!/usr/bin/env python3
"""
server.py — Serveur Flask SailTracker
"""

import gzip
import json
import logging
import logging.handlers
import math
import os
import shutil
import sqlite3
import subprocess
import threading
import uuid
import re
MOBILE_UA = re.compile(r"Android|iPhone|iPad|iPod|Mobile|BlackBerry|IEMobile", re.IGNORECASE)
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory, render_template, make_response

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = BASE_DIR / "sailtracker.db"
STATIC_DIR = BASE_DIR / "static"
TEMPLATE_DIR = BASE_DIR / "templates"
GRIB_CACHE_DIR = STATIC_DIR / "grib_cache"
FLASK_HOST = os.getenv("FLASK_HOST", "127.0.0.1")
FLASK_PORT = int(os.getenv("FLASK_PORT", "8085"))

# Zones de vérification (utilisées par forecast_verifier et model weighting)
VERIF_ZONES = {
    'cabo_verde':     (16.9, -25.0),
    'mid_atlantic':   (15.0, -40.0),
    'caribbean_east': (13.5, -55.0),
    'caribbean_west': (17.9, -62.8),
}

# =============================================================================
# Logging
# =============================================================================

def setup_logging():
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    logger = logging.getLogger("sailtracker_server")
    logger.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "server.log", maxBytes=10*1024*1024, backupCount=5, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)
    return logger

logger = setup_logging()

app = Flask(__name__, static_folder=str(STATIC_DIR), template_folder=str(TEMPLATE_DIR))

# Filtre Jinja2 pour couleur de score
@app.template_filter('score_color')
def score_color_filter(score):
    if score >= 70: return '#3fb950'
    if score >= 50: return '#d29922'
    return '#f85149'

@app.template_filter('score_label')
def score_label_filter(score):
    if score >= 70: return 'GO'
    if score >= 50: return 'MOYEN'
    return 'MAUVAIS'

from flask_cors import CORS
CORS(app, origins=["http://45.55.239.73", "http://localhost", "http://127.0.0.1"])

from briefing import generate_weather_briefing
from polars import get_polar, reload_polar, update_polars_from_observations, PolarDiagram
from routing import isochrone_routing, GribWindProvider
try:
    from rust_engine import engine_available, engine_state, rust_polar, rust_version
    _rust_engine_imported = True
except ImportError:
    _rust_engine_imported = False
    def engine_available(): return False
    def engine_state(): return {"rust_binary_exists": False, "rust_binary_path": "", "last_rust_call": None, "last_rust_duration_ms": None, "last_python_fallback": None, "last_python_command": None}
    def rust_polar(twa, tws): return None
    def rust_version(): return None

# Tâches de routage asynchrones : {task_id: {status, progress, result, error}}
_routing_tasks: dict = {}
_routing_tasks_lock = threading.Lock()

# Instance GribWindProvider (chargée au démarrage)
_wind_provider: GribWindProvider = None

def get_wind_provider() -> GribWindProvider:
    global _wind_provider
    if _wind_provider is None:
        _wind_provider = GribWindProvider()
    return _wind_provider

# =============================================================================
# Helpers
# =============================================================================

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def haversine_nm(lat1, lon1, lat2, lon2):
    R = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

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

def great_circle_waypoints(lat1, lon1, name1, lat2, lon2, name2, spacing_nm=250.0):
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

# =============================================================================
# Pages
# =============================================================================

@app.route("/")
def index():
    ua = request.headers.get('User-Agent', '')
    if MOBILE_UA.search(ua):
        return send_from_directory(str(STATIC_DIR), 'index_mobile.html')
    return send_from_directory(str(STATIC_DIR), 'index.html')

@app.route("/mobile")
def mobile_index():
    return send_from_directory(str(STATIC_DIR), 'index_mobile.html')

@app.route("/passage")
def passage_page():
    return send_from_directory(str(STATIC_DIR), "passage.html")

@app.route("/polars")
def polars_page():
    return render_template("polars.html")

@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(str(STATIC_DIR), filename)

# =============================================================================
# API : positions
# =============================================================================

@app.route("/api/position/latest")
def api_position_latest():
    source = request.args.get("source")
    conn = get_db()
    if source:
        row = conn.execute(
            "SELECT timestamp,latitude,longitude,speed_knots,course,heading,nav_status,source FROM positions WHERE source=? ORDER BY timestamp DESC LIMIT 1",
            (source,)).fetchone()
    else:
        row = conn.execute(
            "SELECT timestamp,latitude,longitude,speed_knots,course,heading,nav_status,source FROM positions ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    conn.close()
    if row is None:
        return jsonify({"error": "Aucune position disponible"}), 404
    return jsonify({
        "timestamp": row["timestamp"], "latitude": row["latitude"],
        "longitude": row["longitude"], "speed_knots": row["speed_knots"],
        "course": row["course"], "heading": row["heading"],
        "nav_status": row["nav_status"], "source": row["source"] or "ais",
    })

@app.route("/api/position/track")
def api_position_track():
    hours = request.args.get("hours", 72, type=int)
    hours = max(1, min(hours, 720))
    source = request.args.get("source")
    conn = get_db()
    if source:
        rows = conn.execute(
            "SELECT timestamp,latitude,longitude,speed_knots,course,source FROM positions WHERE timestamp>=datetime('now',? || ' hours') AND source=? ORDER BY timestamp ASC",
            (f"-{hours}", source)).fetchall()
    else:
        rows = conn.execute(
            "SELECT timestamp,latitude,longitude,speed_knots,course,COALESCE(source,'ais') as source FROM positions WHERE timestamp>=datetime('now',? || ' hours') ORDER BY timestamp ASC",
            (f"-{hours}",)).fetchall()
    conn.close()
    track = [{"timestamp": r["timestamp"], "latitude": r["latitude"], "longitude": r["longitude"],
               "speed_knots": r["speed_knots"], "course": r["course"], "source": r["source"]} for r in rows]
    return jsonify({"track": track, "count": len(track), "hours": hours})

# =============================================================================
# API : status sources
# =============================================================================

@app.route("/api/status")
def api_status():
    conn = get_db()
    ais_row = conn.execute(
        "SELECT timestamp,latitude,longitude FROM positions WHERE source='ais' OR source IS NULL ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    inreach_row = conn.execute(
        "SELECT timestamp,latitude,longitude FROM positions WHERE source='inreach' ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    weather_row = conn.execute("SELECT MAX(collected_at) as last FROM weather_snapshots").fetchone()
    conn.close()

    ais_ts = ais_row["timestamp"] if ais_row else None
    inreach_ts = inreach_row["timestamp"] if inreach_row else None
    active_source = "none"
    if ais_ts and inreach_ts:
        active_source = "ais" if ais_ts >= inreach_ts else "inreach"
    elif ais_ts: active_source = "ais"
    elif inreach_ts: active_source = "inreach"

    return jsonify({
        "active_source": active_source,
        "ais": {"last_timestamp": ais_ts, "age_minutes": minutes_ago(ais_ts),
                "latitude": ais_row["latitude"] if ais_row else None,
                "longitude": ais_row["longitude"] if ais_row else None},
        "inreach": {"last_timestamp": inreach_ts, "age_minutes": minutes_ago(inreach_ts),
                    "latitude": inreach_row["latitude"] if inreach_row else None,
                    "longitude": inreach_row["longitude"] if inreach_row else None},
        "weather": {"last_collected": weather_row["last"] if weather_row else None,
                    "age_minutes": minutes_ago(weather_row["last"]) if weather_row else None},
    })

# =============================================================================
# API : météo
# =============================================================================

@app.route("/api/weather/latest")
def api_weather_latest():
    conn = get_db()
    row = conn.execute("SELECT * FROM weather_snapshots ORDER BY collected_at DESC LIMIT 1").fetchone()
    conn.close()
    if row is None:
        return jsonify({"error": "Aucune donnée météo"}), 404
    return jsonify({
        "collected_at": row["collected_at"],
        "position": {"latitude": row["latitude"], "longitude": row["longitude"]},
        "wind": {"speed_kmh": row["wind_speed_kmh"], "direction_deg": row["wind_direction_deg"], "gusts_kmh": row["wind_gusts_kmh"]},
        "waves": {"height_m": row["wave_height_m"], "direction_deg": row["wave_direction_deg"], "period_s": row["wave_period_s"]},
        "swell": {"height_m": row["swell_height_m"], "direction_deg": row["swell_direction_deg"], "period_s": row["swell_period_s"]},
        "current": {"speed_knots": row["current_speed_knots"], "direction_deg": row["current_direction_deg"]},
    })

@app.route("/api/weather/forecast")
def api_weather_forecast():
    conn = get_db()
    wind_rows = conn.execute(
        "SELECT forecast_time,value1,value2,value3 FROM weather_forecasts WHERE data_type='wind' AND forecast_time>=datetime('now') AND collected_at=(SELECT MAX(collected_at) FROM weather_forecasts WHERE data_type='wind') ORDER BY forecast_time ASC LIMIT 72"
    ).fetchall()
    wave_rows = conn.execute(
        "SELECT forecast_time,value1,value2,value3 FROM weather_forecasts WHERE data_type='wave' AND forecast_time>=datetime('now') AND collected_at=(SELECT MAX(collected_at) FROM weather_forecasts WHERE data_type='wave') ORDER BY forecast_time ASC LIMIT 72"
    ).fetchall()
    conn.close()
    return jsonify({
        "wind_forecast": [{"time": r["forecast_time"], "speed_kmh": r["value1"], "direction_deg": r["value2"], "gusts_kmh": r["value3"]} for r in wind_rows],
        "wave_forecast": [{"time": r["forecast_time"], "height_m": r["value1"], "direction_deg": r["value2"], "period_s": r["value3"]} for r in wave_rows],
    })

# =============================================================================
# API : routes de passage
# =============================================================================

@app.route("/api/routes", methods=["GET"])
def api_routes_list():
    conn = get_db()
    rows = conn.execute(
        "SELECT id,name,boat_speed_avg_knots,max_wind_knots,max_wave_m,max_swell_m,created_at,status,last_computed FROM passage_routes ORDER BY id"
    ).fetchall()
    conn.close()
    return jsonify({"routes": [{
        "id": r["id"], "name": r["name"],
        "boat_speed_avg_knots": r["boat_speed_avg_knots"],
        "max_wind_knots": r["max_wind_knots"],
        "max_wave_m": r["max_wave_m"], "max_swell_m": r["max_swell_m"],
        "created_at": r["created_at"],
        "status": r["status"] or "ready",
        "last_computed": r["last_computed"],
    } for r in rows]})


@app.route("/api/routes", methods=["POST"])
def api_create_route():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Corps JSON requis"}), 400

    name = data.get("name", "")
    boat_speed = float(data.get("boat_speed_avg_knots", 6.0))
    max_wind = float(data.get("max_wind_knots", 30.0))
    max_wave = float(data.get("max_wave_m", 3.0))
    max_swell = float(data.get("max_swell_m", 3.5))
    spacing = float(data.get("waypoint_spacing_nm", 250.0))

    if "waypoints" in data:
        manual_wps = data["waypoints"]
        if len(manual_wps) < 2:
            return jsonify({"error": "Au moins 2 waypoints requis"}), 400
        # Calculer distance totale pour adapter l'espacement automatiquement
        total_nm_est = sum(
            haversine_nm(manual_wps[i-1]["lat"], manual_wps[i-1]["lon"],
                         manual_wps[i]["lat"], manual_wps[i]["lon"])
            for i in range(1, len(manual_wps))
        )
        if total_nm_est < 50:
            spacing = min(spacing, 15.0)
        elif total_nm_est < 200:
            spacing = min(spacing, 40.0)
        elif total_nm_est < 500:
            spacing = min(spacing, 100.0)
        all_waypoints = []
        for i in range(len(manual_wps) - 1):
            wp1 = manual_wps[i]
            wp2 = manual_wps[i + 1]
            segment = great_circle_waypoints(
                float(wp1["lat"]), float(wp1["lon"]), wp1.get("name", f"WP{i+1}"),
                float(wp2["lat"]), float(wp2["lon"]), wp2.get("name", f"WP{i+2}"),
                spacing_nm=spacing,
            )
            if i == 0:
                all_waypoints = segment
            else:
                all_waypoints.extend(segment[1:])
        waypoints = all_waypoints
        if not name:
            name = f"{manual_wps[0].get('name', 'Départ')} → {manual_wps[-1].get('name', 'Arrivée')}"
    else:
        for field in ["start_lat", "start_lon", "end_lat", "end_lon"]:
            if field not in data:
                return jsonify({"error": f"Champ manquant: {field}"}), 400
        start_lat = float(data["start_lat"])
        start_lon = float(data["start_lon"])
        end_lat = float(data["end_lat"])
        end_lon = float(data["end_lon"])
        start_name = data.get("start_name", "Départ")
        end_name = data.get("end_name", "Arrivée")
        if not name:
            name = f"{start_name} → {end_name}"
        waypoints = great_circle_waypoints(
            start_lat, start_lon, start_name,
            end_lat, end_lon, end_name,
            spacing_nm=spacing,
        )

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO passage_routes (name,waypoints,boat_speed_avg_knots,max_wind_knots,max_wave_m,max_swell_m,status) VALUES (?,?,?,?,?,?,'pending')",
        (name, json.dumps(waypoints, ensure_ascii=False), boat_speed, max_wind, max_wave, max_swell),
    )
    route_id = cur.lastrowid
    conn.commit()
    conn.close()

    total_nm = sum(haversine_nm(waypoints[i-1]["lat"], waypoints[i-1]["lon"],
                                waypoints[i]["lat"], waypoints[i]["lon"])
                   for i in range(1, len(waypoints)))

    return jsonify({
        "id": route_id, "name": name, "waypoints": waypoints,
        "total_distance_nm": round(total_nm, 0),
        "estimated_days": round(total_nm / boat_speed / 24, 1),
    }), 201


@app.route("/api/gpx/parse", methods=["POST"])
def api_gpx_parse():
    import xml.etree.ElementTree as ET
    import zipfile, io
    if "file" not in request.files:
        return jsonify({"error": "Fichier requis (GPX, KML ou KMZ)"}), 400
    f = request.files["file"]
    filename = (f.filename or "").lower()

    def _parse_gpx_root(root):
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"
        waypoints = []
        for rte in root.findall(f".//{ns}rte"):
            for rtept in rte.findall(f"{ns}rtept"):
                lat = float(rtept.get("lat"))
                lon = float(rtept.get("lon"))
                name_el = rtept.find(f"{ns}name")
                wname = name_el.text.strip() if name_el is not None and name_el.text else f"WP{len(waypoints)+1}"
                waypoints.append({"lat": round(lat, 5), "lon": round(lon, 5), "name": wname})
        if not waypoints:
            raw = []
            for trk in root.findall(f".//{ns}trk"):
                for seg in trk.findall(f"{ns}trkseg"):
                    for trkpt in seg.findall(f"{ns}trkpt"):
                        lat = float(trkpt.get("lat"))
                        lon = float(trkpt.get("lon"))
                        name_el = trkpt.find(f"{ns}name")
                        wname = name_el.text.strip() if name_el is not None and name_el.text else ""
                        raw.append({"lat": round(lat, 5), "lon": round(lon, 5), "name": wname})
            if len(raw) > 100:
                indices = [int(i * (len(raw) - 1) / 99) for i in range(100)]
                raw = [raw[idx] for idx in indices]
            for i, pt in enumerate(raw):
                if not pt["name"]:
                    pt["name"] = f"WP{i+1}"
            waypoints = raw
        if not waypoints:
            for wpt in root.findall(f".//{ns}wpt"):
                lat = float(wpt.get("lat"))
                lon = float(wpt.get("lon"))
                name_el = wpt.find(f"{ns}name")
                wname = name_el.text.strip() if name_el is not None and name_el.text else f"WP{len(waypoints)+1}"
                waypoints.append({"lat": round(lat, 5), "lon": round(lon, 5), "name": wname})
        return waypoints

    def _parse_kml_root(root):
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"
        waypoints = []
        for pm in root.findall(f".//{ns}Placemark"):
            name_el = pm.find(f"{ns}name")
            wname = name_el.text.strip() if name_el is not None and name_el.text else f"WP{len(waypoints)+1}"
            point = pm.find(f".//{ns}Point")
            if point is not None:
                coords_el = point.find(f"{ns}coordinates")
                if coords_el is not None and coords_el.text:
                    parts = coords_el.text.strip().split(",")
                    if len(parts) >= 2:
                        lon, lat = float(parts[0]), float(parts[1])
                        waypoints.append({"lat": round(lat, 5), "lon": round(lon, 5), "name": wname})
            else:
                # LineString : extract all coordinates as waypoints
                ls = pm.find(f".//{ns}LineString")
                if ls is not None:
                    coords_el = ls.find(f"{ns}coordinates")
                    if coords_el is not None and coords_el.text:
                        raw = []
                        for coord in coords_el.text.strip().split():
                            parts = coord.split(",")
                            if len(parts) >= 2:
                                raw.append({"lat": round(float(parts[1]), 5), "lon": round(float(parts[0]), 5), "name": ""})
                        if len(raw) > 100:
                            indices = [int(i * (len(raw) - 1) / 99) for i in range(100)]
                            raw = [raw[idx] for idx in indices]
                        for i, pt in enumerate(raw):
                            pt["name"] = f"WP{i+1}"
                        waypoints.extend(raw)
        return waypoints

    try:
        waypoints = []
        if filename.endswith(".kmz"):
            raw_bytes = f.read()
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                kml_names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
                if not kml_names:
                    return jsonify({"error": "Aucun fichier KML dans le KMZ"}), 400
                kml_data = zf.read(kml_names[0])
            root = ET.fromstring(kml_data)
            waypoints = _parse_kml_root(root)
        elif filename.endswith(".kml"):
            root = ET.parse(f).getroot()
            waypoints = _parse_kml_root(root)
        else:
            # GPX (défaut)
            root = ET.parse(f).getroot()
            waypoints = _parse_gpx_root(root)

        if not waypoints:
            return jsonify({"error": "Aucun waypoint trouvé dans le fichier"}), 400
        return jsonify({"waypoints": waypoints, "count": len(waypoints)})

    except ET.ParseError as e:
        return jsonify({"error": f"Fichier invalide: {e}"}), 400
    except Exception as e:
        logger.error(f"GPX/KML parse error: {e}")
        return jsonify({"error": "Erreur lors du parsing"}), 500


@app.route("/api/passage/<int:route_id>/info")
def api_passage_info(route_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM passage_routes WHERE id=?", (route_id,)).fetchone()
    if row is None:
        conn.close()
        return jsonify({"error": "Route non trouvée"}), 404
    waypoints = json.loads(row["waypoints"])
    total_nm = sum(haversine_nm(waypoints[i-1]["lat"], waypoints[i-1]["lon"],
                                waypoints[i]["lat"], waypoints[i]["lon"])
                   for i in range(1, len(waypoints)))
    speed_fallback = row["boat_speed_avg_knots"] or 6.0

    # ETA polaire : chercher dans les simulations de départ
    polar_eta_h = None
    avg_polar_speed = None
    used_polars = False
    try:
        best_sim = conn.execute(
            """SELECT summary FROM departure_simulations
               WHERE route_id=? AND overall_score > 0
               ORDER BY computed_at DESC, overall_score DESC LIMIT 1""",
            (route_id,)
        ).fetchone()
        if best_sim and best_sim["summary"]:
            import json as _json
            s = _json.loads(best_sim["summary"])
            polar_eta_h = s.get("adjusted_eta_hours")
            avg_polar_speed = s.get("avg_polar_speed_kts")
            used_polars = bool(s.get("used_polars", False))
    except Exception:
        pass
    conn.close()

    estimated_days_fixed = round(total_nm / speed_fallback / 24, 1)
    estimated_days_polar = round(polar_eta_h / 24, 1) if polar_eta_h else None

    return jsonify({
        "id": row["id"], "name": row["name"], "waypoints": waypoints,
        "boat_speed_avg_knots": speed_fallback,
        "max_wind_knots": row["max_wind_knots"],
        "max_wave_m": row["max_wave_m"], "max_swell_m": row["max_swell_m"],
        "total_distance_nm": round(total_nm, 0),
        "estimated_days": estimated_days_polar if estimated_days_polar else estimated_days_fixed,
        "estimated_days_fixed": estimated_days_fixed,
        "estimated_days_polar": estimated_days_polar,
        "avg_polar_speed_kts": avg_polar_speed,
        "used_polars": used_polars,
        "created_at": row["created_at"],
        "status": row["status"] or "ready",
        "last_computed": row["last_computed"],
    })


@app.route("/api/passage/<int:route_id>/compute", methods=["POST"])
def api_compute_passage(route_id):
    conn = get_db()
    row = conn.execute("SELECT id,status FROM passage_routes WHERE id=?", (route_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Route non trouvée"}), 404
    if row["status"] == "computing":
        conn.close()
        return jsonify({"message": "Calcul déjà en cours", "status": "computing"}), 200

    conn.execute("UPDATE passage_routes SET status='computing' WHERE id=?", (route_id,))
    conn.commit()
    conn.close()

    venv_python = str(BASE_DIR / "venv/bin/python")
    script = str(BASE_DIR / "passage_planner.py")
    log_path = BASE_DIR / "logs/passage.log"
    with open(log_path, "a") as log_f:
        subprocess.Popen(
            [venv_python, script, "--route-id", str(route_id)],
            stdout=log_f, stderr=log_f,
            cwd=str(BASE_DIR),
            start_new_session=True,
        )

    logger.info("Calcul lancé en background pour route ID=%d", route_id)
    return jsonify({"status": "computing", "route_id": route_id}), 202


@app.route("/api/passage/<int:route_id>/compute_status")
def api_compute_status(route_id):
    conn = get_db()
    row = conn.execute(
        "SELECT status,last_computed FROM passage_routes WHERE id=?", (route_id,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Route non trouvée"}), 404
    return jsonify({
        "route_id": route_id,
        "status": row["status"] or "ready",
        "last_computed": row["last_computed"],
    })


@app.route("/api/passage/<int:route_id>/forecast")
def api_passage_forecast(route_id):
    conn = get_db()
    last_row = conn.execute(
        "SELECT MAX(collected_at) as last FROM passage_forecasts WHERE route_id=?", (route_id,)
    ).fetchone()
    if not last_row or not last_row["last"]:
        conn.close()
        return jsonify({"error": "Aucune prévision disponible", "collected_at": None, "waypoints": []}), 200

    collected_at = last_row["last"]
    rows = conn.execute(
        """SELECT waypoint_index,latitude,longitude,forecast_time,
               wind_speed_knots,wind_direction_deg,wind_gusts_knots,
               wave_height_m,wave_direction_deg,wave_period_s,
               swell_height_m,swell_direction_deg,swell_period_s,
               current_speed_knots,current_direction_deg
           FROM passage_forecasts WHERE route_id=? AND collected_at=?
           ORDER BY waypoint_index,forecast_time""",
        (route_id, collected_at),
    ).fetchall()
    conn.close()

    waypoints_data = {}
    for r in rows:
        wp_idx = r["waypoint_index"]
        if wp_idx not in waypoints_data:
            waypoints_data[wp_idx] = {"wp_index": wp_idx, "latitude": r["latitude"],
                                       "longitude": r["longitude"], "forecasts": []}
        waypoints_data[wp_idx]["forecasts"].append({
            "time": r["forecast_time"], "wind_speed_knots": r["wind_speed_knots"],
            "wind_direction_deg": r["wind_direction_deg"], "wind_gusts_knots": r["wind_gusts_knots"],
            "wave_height_m": r["wave_height_m"], "wave_direction_deg": r["wave_direction_deg"],
            "wave_period_s": r["wave_period_s"], "swell_height_m": r["swell_height_m"],
            "swell_direction_deg": r["swell_direction_deg"], "swell_period_s": r["swell_period_s"],
            "current_speed_knots": r["current_speed_knots"],
            "current_direction_deg": r["current_direction_deg"],
        })
    return jsonify({"route_id": route_id, "collected_at": collected_at,
                    "waypoints": list(waypoints_data.values())})


@app.route("/api/passage/<int:route_id>/departures")
def api_passage_departures(route_id):
    conn = get_db()
    last_row = conn.execute(
        "SELECT MAX(computed_at) as last FROM departure_simulations WHERE route_id=?", (route_id,)
    ).fetchone()
    if not last_row or not last_row["last"]:
        conn.close()
        return jsonify({"computed_at": None, "simulations": []}), 200

    computed_at = last_row["last"]
    rows = conn.execute(
        "SELECT departure_date,confidence_score,comfort_score,overall_score,alerts,summary FROM departure_simulations WHERE route_id=? AND computed_at=? ORDER BY departure_date ASC",
        (route_id, computed_at),
    ).fetchall()
    conn.close()

    simulations = []
    for r in rows:
        alerts = []
        if r["alerts"]:
            try: alerts = json.loads(r["alerts"])
            except: alerts = [r["alerts"]]
        overall = r["overall_score"] or 0
        verdict = "GO" if overall >= 70 else ("ATTENTION" if overall >= 45 else "NO-GO")
        summary_data = {}
        if r["summary"]:
            try: summary_data = json.loads(r["summary"])
            except: pass
        simulations.append({
            "departure_date": r["departure_date"],
            "confidence_score": r["confidence_score"],
            "comfort_score": r["comfort_score"],
            "overall_score": overall, "alerts": alerts, "verdict": verdict,
            "current_effect_knots": summary_data.get("current_effect_knots"),
            "adjusted_eta_hours": summary_data.get("adjusted_eta_hours"),
        })
    return jsonify({"route_id": route_id, "computed_at": computed_at, "simulations": simulations})


# =============================================================================
# API : ensemble forecasts (Feature 2)
# =============================================================================

@app.route("/api/passage/<int:route_id>/ensemble")
def api_passage_ensemble(route_id):
    wp_idx = int(request.args.get('wp', 0))
    conn = get_db()

    last_row = conn.execute(
        "SELECT MAX(collected_at) as last FROM ensemble_forecasts WHERE route_id=? AND waypoint_index=?",
        (route_id, wp_idx)
    ).fetchone()

    if not last_row or not last_row["last"]:
        conn.close()
        return jsonify({"available": False, "message": "Pas de données ensemble"}), 200

    collected_at = last_row["last"]
    rows = conn.execute(
        """SELECT member_id, forecast_time, wind_speed_knots, wind_direction_deg
           FROM ensemble_forecasts
           WHERE route_id=? AND waypoint_index=? AND collected_at=?
           ORDER BY forecast_time, member_id""",
        (route_id, wp_idx, collected_at)
    ).fetchall()
    conn.close()

    # Group by time
    times_dict = {}
    for r in rows:
        t = r["forecast_time"]
        if t not in times_dict:
            times_dict[t] = {"speeds": [], "dirs": []}
        if r["wind_speed_knots"] is not None:
            times_dict[t]["speeds"].append(r["wind_speed_knots"])
        if r["wind_direction_deg"] is not None:
            times_dict[t]["dirs"].append(r["wind_direction_deg"])

    sorted_times = sorted(times_dict.keys())

    # Get all members data by member_id
    members_dict = {}
    for r in rows:
        mid = r["member_id"]
        if mid not in members_dict:
            members_dict[mid] = {}
        members_dict[mid][r["forecast_time"]] = r["wind_speed_knots"]

    members_series = []
    for mid in sorted(members_dict.keys()):
        series = [members_dict[mid].get(t) for t in sorted_times]
        members_series.append(series)

    # Calculate stats
    stats = []
    for t in sorted_times:
        speeds = times_dict[t]["speeds"]
        if speeds:
            arr = np.array(speeds)
            stats.append({
                "time": t,
                "mean": float(np.mean(arr)),
                "p10": float(np.percentile(arr, 10)),
                "p25": float(np.percentile(arr, 25)),
                "p75": float(np.percentile(arr, 75)),
                "p90": float(np.percentile(arr, 90)),
                "n": len(speeds),
            })
        else:
            stats.append({"time": t, "mean": None, "p10": None, "p25": None, "p75": None, "p90": None, "n": 0})

    # Reliability indicator (spread over first 72h)
    p90_vals = [s["p90"] for s in stats[:12] if s["p90"] is not None]
    p10_vals = [s["p10"] for s in stats[:12] if s["p10"] is not None]
    if p90_vals and p10_vals:
        spread_avg = float(np.mean(np.array(p90_vals) - np.array(p10_vals)))
        if spread_avg < 5:
            reliability = "Très fiable — convergence modèles"
            reliability_level = "good"
        elif spread_avg < 10:
            reliability = "Fiable — incertitude modérée"
            reliability_level = "medium"
        else:
            reliability = "Incertain — forte divergence"
            reliability_level = "bad"
    else:
        spread_avg = None
        reliability = "Données insuffisantes"
        reliability_level = "unknown"

    return jsonify({
        "available": True,
        "route_id": route_id,
        "waypoint_index": wp_idx,
        "collected_at": collected_at,
        "times": sorted_times,
        "members": members_series,
        "stats": stats,
        "spread_avg_kts": spread_avg,
        "reliability": reliability,
        "reliability_level": reliability_level,
    })


# =============================================================================
# API : GRIB index (Feature 1)
# =============================================================================

@app.route("/api/grib/index")
def api_grib_index():
    index_file = GRIB_CACHE_DIR / "index.json"
    if not index_file.exists():
        return jsonify({"error": "Données GRIB non disponibles", "runs": []}), 200
    try:
        with open(index_file) as f:
            data = json.load(f)
        return jsonify(data)
    except Exception as e:
        logger.error("Erreur lecture grib index: %s", e)
        return jsonify({"error": str(e), "runs": []}), 500



# =============================================================================
# API : Polaires
# =============================================================================

@app.route("/api/polars", methods=["GET"])
def api_polars_get():
    try:
        return jsonify(get_polar().to_dict())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/polars", methods=["PUT"])
def api_polars_update():
    data = request.get_json()
    twa = data.get("twa")
    tws = data.get("tws")
    speed = data.get("speed")
    if twa is None or tws is None or speed is None:
        return jsonify({"error": "twa, tws, speed requis"}), 400
    try:
        p = get_polar()
        p.update_speed(float(twa), float(tws), float(speed))
        p.save()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/polars/reset", methods=["POST"])
def api_polars_reset():
    try:
        src = BASE_DIR / "data" / "polars" / "pollen1_default.csv"
        dst = BASE_DIR / "data" / "polars" / "pollen1.csv"
        shutil.copy2(str(src), str(dst))
        reload_polar()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/polars/export")
def api_polars_export():
    polar_path = BASE_DIR / "data" / "polars" / "pollen1.csv"
    response = make_response(polar_path.read_text(encoding="utf-8"))
    response.headers["Content-Type"] = "text/csv"
    response.headers["Content-Disposition"] = "attachment; filename=pollen1.csv"
    return response

@app.route("/api/polars/speed")
def api_polars_speed():
    try:
        twa = float(request.args.get("twa", 0))
        tws = float(request.args.get("tws", 0))
        speed = get_polar().get_boat_speed(twa, tws)
        return jsonify({"twa": twa, "tws": tws, "boat_speed_kts": round(speed, 2)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/polars/observations")
def api_polars_observations():
    try:
        conn = get_db()
        rows = conn.execute("""
            SELECT id, timestamp, twa_deg, tws_kts, stw_kts, latitude, longitude, is_valid
            FROM polar_observations
            WHERE is_valid = 1
            ORDER BY timestamp DESC LIMIT 500
        """).fetchall()
        conn.close()
        return jsonify({"observations": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"observations": [], "error": str(e)})

@app.route("/api/polars/comparison")
def api_polars_comparison():
    try:
        conn = get_db()
        rows = conn.execute("""
            SELECT twa_deg, tws_kts, stw_kts FROM polar_observations WHERE is_valid=1
        """).fetchall()
        conn.close()
        p = get_polar()
        diffs = []
        total_sq = 0.0
        for r in rows:
            twa, tws, stw = r["twa_deg"], r["tws_kts"], r["stw_kts"]
            theoretical = p.get_boat_speed(twa, tws)
            diff = stw - theoretical
            diffs.append({"twa": round(twa, 1), "tws": round(tws, 1),
                          "observed": round(stw, 2), "theoretical": round(theoretical, 2),
                          "diff": round(diff, 2)})
            total_sq += diff ** 2
        rmse = math.sqrt(total_sq / len(diffs)) if diffs else 0
        return jsonify({"comparison": diffs, "rmse": round(rmse, 3), "n": len(diffs)})
    except Exception as e:
        return jsonify({"comparison": [], "error": str(e)})

# =============================================================================
# API : Routage isochrones
# =============================================================================

@app.route("/api/passage/routes/<int:route_id>/optimize", methods=["POST"])
def api_optimize_route(route_id):
    data = request.get_json() or {}
    departure_str = data.get("departure", "")
    try:
        if departure_str:
            departure_dt = datetime.fromisoformat(departure_str.replace("Z", "+00:00"))
        else:
            departure_dt = datetime.now(timezone.utc)
    except Exception:
        return jsonify({"error": "Format departure invalide (ISO8601)"}), 400

    conn = get_db()
    route = conn.execute("SELECT * FROM passage_routes WHERE id=?", (route_id,)).fetchone()
    conn.close()
    if not route:
        return jsonify({"error": "Route introuvable"}), 404

    try:
        waypoints = json.loads(route["waypoints"])
    except Exception:
        return jsonify({"error": "Waypoints invalides"}), 400

    if len(waypoints) < 2:
        return jsonify({"error": "La route doit avoir au moins 2 waypoints"}), 400

    task_id = str(uuid.uuid4())
    with _routing_tasks_lock:
        _routing_tasks[task_id] = {"status": "computing", "progress": 0, "result": None, "error": None}

    def run_routing():
        try:
            polar = get_polar()
            wind_prov = get_wind_provider()
            start = (waypoints[0]["lat"], waypoints[0]["lon"])
            end = (waypoints[-1]["lat"], waypoints[-1]["lon"])
            result = isochrone_routing(start, end, departure_dt, polar, wind_prov)
            # Sauvegarder le résultat en DB
            db2 = get_db()
            db2.execute(
                """INSERT INTO route_optimizations (route_id, computed_at, departure, result_json)
                   VALUES (?, datetime('now'), ?, ?)""",
                (route_id, departure_str, json.dumps(result))
            )
            db2.commit()
            db2.close()
            with _routing_tasks_lock:
                _routing_tasks[task_id]["status"] = "done"
                _routing_tasks[task_id]["result"] = result
                _routing_tasks[task_id]["progress"] = 100
        except Exception as ex:
            logger.error("Erreur routage tâche %s: %s", task_id, ex)
            with _routing_tasks_lock:
                _routing_tasks[task_id]["status"] = "error"
                _routing_tasks[task_id]["error"] = str(ex)

    threading.Thread(target=run_routing, daemon=True).start()
    return jsonify({"task_id": task_id, "status": "computing"})

@app.route("/api/passage/routes/<int:route_id>/optimize/status")
def api_optimize_status(route_id):
    task_id = request.args.get("task_id", "")
    with _routing_tasks_lock:
        task = _routing_tasks.get(task_id)
    if not task:
        return jsonify({"error": "Tâche inconnue"}), 404
    return jsonify({"status": task["status"], "progress": task["progress"], "error": task.get("error")})

@app.route("/api/passage/routes/<int:route_id>/optimize/result")
def api_optimize_result(route_id):
    task_id = request.args.get("task_id", "")
    with _routing_tasks_lock:
        task = _routing_tasks.get(task_id)
    if not task:
        return jsonify({"error": "Tâche inconnue"}), 404
    if task["status"] != "done":
        return jsonify({"error": f"Calcul en cours ({task['status']})"}), 202
    return jsonify(task["result"])

# =============================================================================
# API : supprimer une route
# =============================================================================

@app.route("/api/passage/routes/<int:route_id>/move-waypoint", methods=["POST"])
def api_move_waypoint(route_id):
    """Met à jour la position d'un waypoint (index dans le tableau JSON)."""
    data = request.get_json() or {}
    idx  = data.get("index")
    lat  = data.get("lat")
    lon  = data.get("lon")
    if idx is None or lat is None or lon is None:
        return jsonify({"success": False, "error": "index, lat et lon requis"}), 400
    try:
        idx = int(idx)
        lat = float(lat)
        lon = float(lon)
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "Valeurs invalides"}), 400
    conn = get_db()
    try:
        row = conn.execute("SELECT waypoints FROM passage_routes WHERE id=?", (route_id,)).fetchone()
        if not row:
            return jsonify({"success": False, "error": "Route non trouvée"}), 404
        wps = json.loads(row["waypoints"])
        if idx < 0 or idx >= len(wps):
            return jsonify({"success": False, "error": f"Index {idx} hors limites"}), 400
        wps[idx]["lat"] = round(lat, 6)
        wps[idx]["lon"] = round(lon, 6)
        # Recalculer distance totale
        total_nm = sum(
            haversine_nm(wps[i-1]["lat"], wps[i-1]["lon"], wps[i]["lat"], wps[i]["lon"])
            for i in range(1, len(wps))
        )
        conn.execute("UPDATE passage_routes SET waypoints=? WHERE id=?",
                     (json.dumps(wps, ensure_ascii=False), route_id))
        conn.commit()
        logger.info("Route %d WP%d déplacé vers (%.4f, %.4f)", route_id, idx, lat, lon)
        return jsonify({"success": True, "index": idx, "lat": lat, "lon": lon,
                        "total_nm": round(total_nm, 1)})
    except Exception as e:
        logger.error("move-waypoint route %d: %s", route_id, e)
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/passage/routes/<int:route_id>/rename", methods=["POST"])
def api_rename_route(route_id):
    data = request.get_json() or {}
    new_name = data.get("name", "").strip()
    if not new_name:
        return jsonify({"success": False, "error": "Le nom ne peut pas être vide"}), 400
    if len(new_name) > 100:
        return jsonify({"success": False, "error": "Nom trop long (max 100 caractères)"}), 400
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM passage_routes WHERE id=?", (route_id,)).fetchone()
        if not row:
            return jsonify({"success": False, "error": "Route non trouvée"}), 404
        conn.execute("UPDATE passage_routes SET name=? WHERE id=?", (new_name, route_id))
        conn.commit()
        return jsonify({"success": True, "name": new_name})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/passage/routes/<int:route_id>/delete", methods=["POST"])
def api_delete_route(route_id):
    conn = get_db()
    try:
        row = conn.execute("SELECT id, name FROM passage_routes WHERE id=?", (route_id,)).fetchone()
        if not row:
            return jsonify({"success": False, "error": "Route non trouvée"}), 404
        # Supprimer toutes les données associées
        conn.execute("DELETE FROM passage_forecasts WHERE route_id=?", (route_id,))
        conn.execute("DELETE FROM ensemble_forecasts WHERE route_id=?", (route_id,))
        conn.execute("DELETE FROM departure_simulations WHERE route_id=?", (route_id,))
        conn.execute("DELETE FROM passage_routes WHERE id=?", (route_id,))
        conn.commit()
        logger.info("Route %d ('%s') supprimée avec toutes ses données", route_id, row["name"])
        return jsonify({"success": True, "message": f"Route '{row['name']}' supprimée"})
    except Exception as e:
        logger.error("Erreur suppression route %d: %s", route_id, e)
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        conn.close()


# =============================================================================
# API : vent GRIB (wind-grid)
# =============================================================================

@app.route("/api/passage/wind-grid")
def api_passage_wind_grid():
    """Retourne une grille de vecteurs vent depuis les fichiers GRIB."""
    import json as _json

    run_param = request.args.get('run')
    fh_param  = request.args.get('fh', 'f000')
    route_id_param = request.args.get('route_id')

    # Lire index GRIB
    index_file = GRIB_CACHE_DIR / "index.json"
    if not index_file.exists():
        return jsonify({"error": "Données GRIB non disponibles", "grid": []}), 200
    try:
        with open(index_file) as f:
            idx = _json.load(f)
    except Exception as e:
        return jsonify({"error": str(e), "grid": []}), 500

    runs = idx.get('runs', [])
    if not runs:
        return jsonify({"error": "Aucun run disponible", "grid": []}), 200

    # Construire la liste des temps disponibles
    available_times = []
    for r in runs:
        run_id = r['run']
        fh_labels = r.get('fh_labels', [])
        valid_times = r.get('valid_times', [])
        for i, fh in enumerate(fh_labels):
            vt = valid_times[i] if i < len(valid_times) else ''
            available_times.append({'run': run_id, 'fh': fh, 'valid_time': vt})

    # Sélectionner le run
    selected_run = run_param or runs[-1]['run']
    run_info = next((r for r in runs if r['run'] == selected_run), runs[-1])

    # Vérifier que fh existe
    fh_labels = run_info.get('fh_labels', ['f000'])
    if fh_param not in fh_labels:
        fh_param = fh_labels[0]

    # Trouver valid_time pour ce fh
    fh_idx = fh_labels.index(fh_param)
    valid_times = run_info.get('valid_times', [])
    forecast_time = valid_times[fh_idx] if fh_idx < len(valid_times) else ''

    # Lire le fichier wind
    wind_file = GRIB_CACHE_DIR / f"wind_{selected_run}_{fh_param}.json"
    if not wind_file.exists():
        return jsonify({"error": f"Fichier GRIB non trouvé: {wind_file.name}", "grid": []}), 200
    try:
        with open(wind_file) as f:
            grib_data = _json.load(f)
    except Exception as e:
        return jsonify({"error": str(e), "grid": []}), 500

    # Trouver composantes U et V
    u_entry = next((e for e in grib_data if e['header'].get('parameterNumber') == 2), None)
    v_entry = next((e for e in grib_data if e['header'].get('parameterNumber') == 3), None)
    if not u_entry or not v_entry:
        return jsonify({"error": "Composantes U/V non trouvées", "grid": []}), 200

    hdr = u_entry['header']
    la1 = hdr['la1']   # 35°N
    lo1 = hdr['lo1']   # -85°W
    la2 = hdr['la2']   # -5°S
    lo2 = hdr['lo2']   # 5°E
    dx  = hdr['dx']    # 0.25
    dy  = hdr['dy']    # 0.25
    nx  = hdr['nx']    # 361
    ny  = hdr['ny']    # 161
    u_data = u_entry['data']
    v_data = v_entry['data']

    # Bbox : depuis waypoints route ±3° ou Atlantique par défaut
    lat_min_box, lat_max_box = 8.0, 22.0
    lon_min_box, lon_max_box = -68.0, -18.0

    if route_id_param:
        try:
            conn = get_db()
            row = conn.execute("SELECT waypoints FROM passage_routes WHERE id=?", (int(route_id_param),)).fetchone()
            conn.close()
            if row:
                wps = _json.loads(row['waypoints'])
                lats = [w['lat'] for w in wps]
                lons = [w['lon'] for w in wps]
                lat_min_box = min(lats) - 3
                lat_max_box = max(lats) + 3
                lon_min_box = min(lons) - 3
                lon_max_box = max(lons) + 3
        except Exception as e:
            logger.warning("wind-grid: impossible de lire la route %s: %s", route_id_param, e)

    # Clamp aux limites de la grille
    lat_min_box = max(lat_min_box, la2)
    lat_max_box = min(lat_max_box, la1)
    lon_min_box = max(lon_min_box, lo1)
    lon_max_box = min(lon_max_box, lo2)

    # Adapter la résolution selon la longueur de la route
    if route_id_param:
        try:
            _c = get_db()
            _row = _c.execute("SELECT waypoints FROM passage_routes WHERE id=?", (int(route_id_param),)).fetchone()
            _c.close()
            wps_for_dist = _json.loads(_row["waypoints"]) if _row else []
            route_dist_nm = sum(
                math.sqrt((wps_for_dist[i]["lat"]-wps_for_dist[i-1]["lat"])**2 +
                          (wps_for_dist[i]["lon"]-wps_for_dist[i-1]["lon"])**2) * 60
                for i in range(1, len(wps_for_dist))
            ) if len(wps_for_dist) > 1 else 2000
        except Exception:
            route_dist_nm = 2000
    else:
        route_dist_nm = 2000
    if route_dist_nm < 100:
        SKIP = 2   # ~0.5°
    elif route_dist_nm < 500:
        SKIP = 4   # ~1°
    else:
        SKIP = 8   # ~2°
    grid = []
    lat_i_min = max(0, round((la1 - lat_max_box) / dy))
    lat_i_max = min(ny - 1, round((la1 - lat_min_box) / dy))
    lon_i_min = max(0, round((lon_min_box - lo1) / dx))
    lon_i_max = min(nx - 1, round((lon_max_box - lo1) / dx))

    for lat_i in range(lat_i_min, lat_i_max + 1, SKIP):
        for lon_i in range(lon_i_min, lon_i_max + 1, SKIP):
            flat_i = lat_i * nx + lon_i
            if flat_i >= len(u_data) or flat_i >= len(v_data):
                continue
            u = u_data[flat_i]
            v = v_data[flat_i]
            speed_ms = math.sqrt(u * u + v * v)
            speed_kts = speed_ms * 1.94384
            dir_met = (270 - math.degrees(math.atan2(v, u))) % 360
            lat = round(la1 - lat_i * dy, 2)
            lon = round(lo1 + lon_i * dx, 2)
            grid.append({'lat': lat, 'lon': lon, 'speed_kts': round(speed_kts, 1), 'dir': round(dir_met, 0)})

    return jsonify({
        'run': selected_run,
        'fh': fh_param,
        'forecast_time': forecast_time,
        'available_times': available_times,
        'grid': grid,
    })


# =============================================================================
# API : briefing météo passage
# =============================================================================

@app.route("/api/passage/<int:route_id>/briefing")
def api_passage_briefing(route_id):
    """Génère un briefing météo en langage marin pour la route."""
    import json as _json

    conn = get_db()
    try:
        # Route + waypoints
        route_row = conn.execute("SELECT waypoints, boat_speed_avg_knots FROM passage_routes WHERE id=?", (route_id,)).fetchone()
        if not route_row:
            return jsonify({"error": "Route non trouvée"}), 404

        waypoints = _json.loads(route_row['waypoints'])
        boat_speed = route_row['boat_speed_avg_knots'] or 6.0

        # Meilleur départ
        best_dep = conn.execute(
            "SELECT departure_date, overall_score FROM departure_simulations WHERE route_id=? ORDER BY overall_score DESC LIMIT 1",
            (route_id,)
        ).fetchone()
        best_departure_date = best_dep['departure_date'] if best_dep else None
        best_score = best_dep['overall_score'] if best_dep else None

        # Agréger passage_forecasts par waypoint_index (moyennes conditions typiques)
        agg_rows = conn.execute(
            """SELECT waypoint_index,
                      AVG(latitude) as lat, AVG(longitude) as lon,
                      AVG(wind_speed_knots) as wind_speed,
                      AVG(wind_direction_deg) as wind_dir,
                      AVG(wave_height_m) as wave_height,
                      AVG(current_speed_knots) as current_speed
               FROM passage_forecasts
               WHERE route_id=?
               GROUP BY waypoint_index
               ORDER BY waypoint_index""",
            (route_id,)
        ).fetchall()

        if not agg_rows:
            return jsonify({"error": "Aucune prévision disponible pour cette route", "summary": "", "phases": [], "alerts": []}), 200

    finally:
        conn.close()

    # Calcul distances cumulées
    total_nm = 0.0
    nm_cumul = []
    for i, wp in enumerate(waypoints):
        if i == 0:
            nm_cumul.append(0.0)
        else:
            prev = waypoints[i - 1]
            total_nm += haversine_nm(prev['lat'], prev['lon'], wp['lat'], wp['lon'])
            nm_cumul.append(total_nm)

    # Cap global (premier → dernier WP)
    if len(waypoints) >= 2:
        from briefing import bearing as _bearing
        route_bearing = _bearing(waypoints[0]['lat'], waypoints[0]['lon'], waypoints[-1]['lat'], waypoints[-1]['lon'])
    else:
        route_bearing = 0.0

    # Construire waypoints_data pour generate_weather_briefing
    waypoints_data = []
    for row in agg_rows:
        wp_idx = row['waypoint_index']
        nm_from_start = nm_cumul[wp_idx] if wp_idx < len(nm_cumul) else 0.0
        waypoints_data.append({
            'lat': row['lat'],
            'lon': row['lon'],
            'wind_speed': row['wind_speed'],
            'wind_dir': row['wind_dir'],
            'wave_height': row['wave_height'],
            'current_speed': row['current_speed'],
            'nm_from_start': nm_from_start,
        })

    route_info = {'total_nm': total_nm, 'route_bearing': route_bearing}
    briefing = generate_weather_briefing(waypoints_data, route_info, best_departure_date, best_score)
    briefing['best_departure_date'] = best_departure_date
    briefing['best_score'] = best_score

    return jsonify(briefing)


# =============================================================================
# API : passage summary (Feature 3 - lite page)
# =============================================================================

def build_passage_summary():
    conn = get_db()
    try:
        route_row = conn.execute(
            "SELECT id, name, boat_speed_avg_knots FROM passage_routes WHERE status='ready' ORDER BY id LIMIT 1"
        ).fetchone()
        if not route_row:
            return None

        route_id = route_row["id"]
        route_name = route_row["name"]
        boat_speed = route_row["boat_speed_avg_knots"]

        # Get latest departure simulations
        last_row = conn.execute(
            "SELECT MAX(computed_at) as last FROM departure_simulations WHERE route_id=?",
            (route_id,)
        ).fetchone()
        if not last_row or not last_row["last"]:
            return None

        computed_at = last_row["last"]
        sim_rows = conn.execute(
            "SELECT departure_date, confidence_score, overall_score, alerts, summary FROM departure_simulations WHERE route_id=? AND computed_at=? ORDER BY departure_date ASC LIMIT 7",
            (route_id, computed_at)
        ).fetchall()

        # Get route info for segments
        route_info_row = conn.execute("SELECT waypoints FROM passage_routes WHERE id=?", (route_id,)).fetchone()
        waypoints = json.loads(route_info_row["waypoints"]) if route_info_row else []

        # Get latest forecast for segment summaries
        last_fc_row = conn.execute(
            "SELECT MAX(collected_at) as last FROM passage_forecasts WHERE route_id=?", (route_id,)
        ).fetchone()
        segments = []
        if last_fc_row and last_fc_row["last"] and waypoints:
            fc_rows = conn.execute(
                """SELECT waypoint_index, wind_speed_knots, wave_height_m, current_speed_knots, current_direction_deg
                   FROM passage_forecasts WHERE route_id=? AND collected_at=?
                   AND forecast_time BETWEEN datetime('now') AND datetime('now', '+72 hours')
                   ORDER BY waypoint_index, forecast_time""",
                (route_id, last_fc_row["last"])
            ).fetchall()
            wp_data = {}
            for r in fc_rows:
                wi = r["waypoint_index"]
                if wi not in wp_data:
                    wp_data[wi] = {"winds": [], "waves": [], "currents": []}
                if r["wind_speed_knots"] is not None:
                    wp_data[wi]["winds"].append(r["wind_speed_knots"])
                if r["wave_height_m"] is not None:
                    wp_data[wi]["waves"].append(r["wave_height_m"])
                if r["current_speed_knots"] is not None:
                    wp_data[wi]["currents"].append(r["current_speed_knots"])

            # Group into ~250 NM segments
            total_nm = sum(haversine_nm(waypoints[i-1]["lat"], waypoints[i-1]["lon"],
                                        waypoints[i]["lat"], waypoints[i]["lon"])
                          for i in range(1, len(waypoints)))
            segment_size = max(1, len(waypoints) // 4)
            for seg_i in range(0, len(waypoints)-1, segment_size):
                seg_wps = list(range(seg_i, min(seg_i + segment_size, len(waypoints)-1)))
                seg_winds = []
                seg_waves = []
                seg_currents = []
                seg_nm_start = sum(haversine_nm(waypoints[j-1]["lat"], waypoints[j-1]["lon"],
                                               waypoints[j]["lat"], waypoints[j]["lon"])
                                  for j in range(1, seg_i+1)) if seg_i > 0 else 0
                seg_nm_end = sum(haversine_nm(waypoints[j-1]["lat"], waypoints[j-1]["lon"],
                                              waypoints[j]["lat"], waypoints[j]["lon"])
                                 for j in range(1, min(seg_i + segment_size, len(waypoints))+1))
                for wi in seg_wps:
                    if wi in wp_data:
                        seg_winds.extend(wp_data[wi]["winds"])
                        seg_waves.extend(wp_data[wi]["waves"])
                        seg_currents.extend(wp_data[wi]["currents"])
                segments.append({
                    "nm_start": round(seg_nm_start),
                    "nm_end": round(min(seg_nm_end, total_nm)),
                    "wind_avg": round(float(np.mean(seg_winds)), 1) if seg_winds else None,
                    "wave_avg": round(float(np.mean(seg_waves)), 2) if seg_waves else None,
                    "current_avg": round(float(np.mean(seg_currents)), 1) if seg_currents else None,
                })

        # Build departures list
        departures = []
        best_score = -1
        best_dep = None
        for r in sim_rows:
            overall = r["overall_score"] or 0
            alerts = []
            if r["alerts"]:
                try: alerts = json.loads(r["alerts"])[:2]
                except: pass
            summary_d = {}
            if r["summary"]:
                try: summary_d = json.loads(r["summary"])
                except: pass
            label = "GO" if overall >= 70 else ("MOYEN" if overall >= 50 else "MAUVAIS")
            dep = {
                "date": r["departure_date"][:10],
                "score": round(overall),
                "label": label,
                "alerts": alerts,
                "adjusted_eta_hours": summary_d.get("adjusted_eta_hours"),
            }
            departures.append(dep)
            if overall > best_score:
                best_score = overall
                best_dep = dep

        # Model accuracy
        acc_rows = conn.execute(
            "SELECT model, AVG(wind_speed_error_avg) as avg_err, COUNT(*) as n FROM model_accuracy WHERE date >= date('now','-30 days') GROUP BY model ORDER BY avg_err ASC LIMIT 1"
        ).fetchall()
        best_model = None
        if acc_rows:
            r = acc_rows[0]
            best_model = {"model": r["model"], "error_kts": round(r["avg_err"], 1), "n_days": r["n"]}

        # Model agreement (how many models agree on best departure)
        model_agreement = None
        if best_dep:
            model_rows = conn.execute(
                "SELECT COUNT(DISTINCT model) as n FROM passage_forecasts WHERE route_id=? AND collected_at=?",
                (route_id, last_fc_row["last"] if last_fc_row and last_fc_row["last"] else "")
            ).fetchone()
            if model_rows:
                model_agreement = model_rows["n"]

        return {
            "route_name": route_name,
            "computed_at": computed_at,
            "best_departure": best_dep,
            "departures": departures,
            "segments": segments,
            "best_model": best_model,
            "model_agreement": model_agreement,
        }
    finally:
        conn.close()


@app.route("/api/passage/summary")
def api_passage_summary():
    data = build_passage_summary()
    if not data:
        return jsonify({"error": "Pas de données disponibles"}), 404
    resp_data = json.dumps(data, ensure_ascii=False)
    response = make_response(gzip.compress(resp_data.encode('utf-8')))
    response.headers['Content-Encoding'] = 'gzip'
    response.headers['Content-Type'] = 'application/json'
    return response


@app.route("/passage/lite")
def passage_lite():
    data = build_passage_summary()
    return render_template('passage_lite.html', data=data)


# =============================================================================
# Page accuracy (Feature 4)
# =============================================================================

@app.route("/accuracy")
def accuracy_page():
    conn = get_db()
    try:
        # Count days of data
        count_row = conn.execute(
            "SELECT COUNT(DISTINCT date) as n FROM model_accuracy"
        ).fetchone()
        days_count = count_row["n"] if count_row else 0

        # Get last 30 days of data grouped by model/zone/horizon
        acc_rows = conn.execute(
            """SELECT model, zone, forecast_hour,
                      AVG(wind_speed_error_avg) as avg_err,
                      AVG(wind_dir_error_avg) as avg_dir_err,
                      COUNT(*) as n_days
               FROM model_accuracy
               WHERE date >= date('now', '-30 days')
               GROUP BY model, zone, forecast_hour
               ORDER BY zone, forecast_hour, avg_err ASC"""
        ).fetchall()

        # Organize data
        zones = list(VERIF_ZONES.keys())
        horizons = [1, 2, 3, 5, 7]
        models = ['ecmwf_ifs025', 'gfs_seamless', 'icon_seamless']

        # Best model table: zones x horizons
        best_table = {}
        model_errors = {}  # model_errors[zone][model][horizon] = err

        for r in acc_rows:
            z = r["zone"]
            m = r["model"]
            h = round(r["forecast_hour"] / 24)  # convert hours to days
            err = r["avg_err"]
            if z not in model_errors:
                model_errors[z] = {}
            if m not in model_errors[z]:
                model_errors[z][m] = {}
            if err is not None:
                model_errors[z][m][h] = round(err, 2)

        for z in zones:
            best_table[z] = {}
            for h in horizons:
                best_entry = None
                best_err = float('inf')
                for m in models:
                    err = model_errors.get(z, {}).get(m, {}).get(h)
                    if err is not None and err < best_err:
                        best_err = err
                        best_entry = {"model": m, "error": err}
                best_table[z][h] = best_entry

        # Chart data: for each zone, errors by model and horizon
        chart_data = {}
        for z in zones:
            chart_data[z] = {}
            for m in models:
                chart_data[z][m] = [model_errors.get(z, {}).get(m, {}).get(h) for h in horizons]

        return render_template('accuracy.html',
            days_count=days_count,
            zones=zones,
            horizons=horizons,
            models=models,
            best_table=best_table,
            chart_data=chart_data,
            model_errors=model_errors,
        )
    finally:
        conn.close()


# =============================================================================
# API : stats
# =============================================================================

@app.route("/api/stats")
def api_stats():
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) as total,MIN(timestamp) as first_ts,MAX(timestamp) as last_ts,MAX(speed_knots) as max_speed FROM positions"
    ).fetchone()
    if not row or not row["total"]:
        conn.close()
        return jsonify({"error": "Aucune donnée"}), 404
    all_pos = conn.execute("SELECT latitude,longitude FROM positions ORDER BY timestamp ASC").fetchall()
    distance_nm = sum(haversine_nm(all_pos[i-1]["latitude"], all_pos[i-1]["longitude"],
                                    all_pos[i]["latitude"], all_pos[i]["longitude"])
                      for i in range(1, len(all_pos)))
    avg_row = conn.execute("SELECT AVG(speed_knots) FROM positions WHERE speed_knots>0.5").fetchone()
    conn.close()
    return jsonify({
        "distance_nm": round(distance_nm, 1),
        "avg_speed_knots": round(avg_row[0] or 0, 1),
        "max_speed_knots": round(row["max_speed"] or 0, 1),
        "tracking_since": row["first_ts"], "last_update": row["last_ts"],
        "total_positions": row["total"],
    })

@app.route("/api/engine/status")
def api_engine_status():
    """Statut et benchmark du moteur de calcul Rust."""
    import time as _time
    state = engine_state()

    # Version du binaire
    ver = rust_version() if state["rust_binary_exists"] else None

    # Benchmark polar TWA=90 TWS=15 sur Rust
    bench_rust_ms = None
    bench_python_ms = None

    if state["rust_binary_exists"]:
        try:
            t0 = _time.monotonic()
            rust_polar(90.0, 15.0)
            bench_rust_ms = round((_time.monotonic() - t0) * 1000, 1)
        except Exception:
            pass

    # Benchmark Python (polaire interne)
    try:
        t0 = _time.monotonic()
        get_polar().get_boat_speed(90.0, 15.0)
        bench_python_ms = round((_time.monotonic() - t0) * 1000, 1)
    except Exception:
        pass

    active = engine_available()
    return jsonify({
        "engine": "rust" if active else "python",
        "rust_binary_exists": state["rust_binary_exists"],
        "rust_binary_path": state["rust_binary_path"],
        "rust_version": ver,
        "last_rust_call": state["last_rust_call"],
        "last_rust_duration_ms": state["last_rust_duration_ms"],
        "last_python_fallback": state["last_python_fallback"],
        "benchmark": {
            "rust_ms": bench_rust_ms,
            "python_ms": bench_python_ms,
        }
    })


@app.route("/api/at-sea")
def api_at_sea():
    """Détecte si le bateau est en navigation active sur une route connue."""
    conn = get_db()

    # Dernière position InReach
    pos = conn.execute(
        "SELECT timestamp, latitude, longitude, speed_knots FROM positions WHERE source='inreach' ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    if not pos:
        conn.close()
        return jsonify({"at_sea": False, "reason": "Aucune position InReach"})

    age_min = minutes_ago(pos["timestamp"])
    if age_min is None or age_min > 120:
        conn.close()
        return jsonify({"at_sea": False, "reason": f"Position trop ancienne ({age_min} min)", "age_min": age_min})

    speed = pos["speed_knots"] or 0
    if speed < 1.0:
        conn.close()
        return jsonify({"at_sea": False, "reason": f"Vitesse trop faible ({speed:.1f} kts)", "speed_knots": speed})

    lat, lon = float(pos["latitude"]), float(pos["longitude"])

    # Route la plus proche (< 50 NM d'un waypoint)
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
        conn.close()
        return jsonify({"at_sea": False, "reason": f"Position trop loin d'une route connue ({best_dist:.0f} NM)"})

    wps = json.loads(best_route["waypoints"])

    # Waypoint le plus proche pour estimer la progression
    nearest_idx = 0
    min_wp_dist = 999.0
    for i, wp in enumerate(wps):
        d = haversine_nm(lat, lon, wp["lat"], wp["lon"])
        if d < min_wp_dist:
            min_wp_dist = d
            nearest_idx = i

    total_dist = sum(haversine_nm(wps[i]["lat"], wps[i]["lon"], wps[i+1]["lat"], wps[i+1]["lon"])
                     for i in range(len(wps)-1))
    dist_covered = sum(haversine_nm(wps[i]["lat"], wps[i]["lon"], wps[i+1]["lat"], wps[i+1]["lon"])
                       for i in range(nearest_idx))
    progress_pct = round(dist_covered / total_dist * 100) if total_dist > 0 else 0
    dist_remaining = sum(haversine_nm(wps[i]["lat"], wps[i]["lon"], wps[i+1]["lat"], wps[i+1]["lon"])
                         for i in range(nearest_idx, len(wps)-1))
    dist_remaining = round(dist_remaining + min_wp_dist, 1)

    # Vitesse moyenne sur les 6 dernières heures
    speeds_6h = conn.execute(
        "SELECT speed_knots FROM positions WHERE source='inreach' AND speed_knots > 0 AND timestamp >= datetime('now','-6 hours')"
    ).fetchall()
    avg_speed = (sum(r["speed_knots"] for r in speeds_6h) / len(speeds_6h)) if speeds_6h else speed

    eta_str = None
    hours_remaining = None
    if avg_speed > 0:
        from datetime import timedelta
        hours_remaining = round(dist_remaining / avg_speed, 1)
        eta_dt = datetime.now(timezone.utc) + timedelta(hours=hours_remaining)
        eta_str = eta_dt.strftime("%d/%m %Hh%M UTC")

    # Conditions météo actuelles
    wx = conn.execute(
        "SELECT wind_speed_kmh, wind_direction_deg, wave_height_m FROM weather_snapshots ORDER BY collected_at DESC LIMIT 1"
    ).fetchone()
    weather_summary = None
    if wx:
        wind_kts = round((wx["wind_speed_kmh"] or 0) / 1.852, 1)
        weather_summary = {
            "wind_knots": wind_kts,
            "wind_dir": wx["wind_direction_deg"],
            "wave_m": wx["wave_height_m"],
        }

    conn.close()

    return jsonify({
        "at_sea": True,
        "route_id": best_route["id"],
        "route_name": best_route["name"],
        "progress_pct": progress_pct,
        "distance_remaining_nm": dist_remaining,
        "eta": eta_str,
        "hours_remaining": hours_remaining,
        "actual_speed_knots": round(avg_speed, 1),
        "position": {"lat": lat, "lon": lon},
        "position_age_min": age_min,
        "weather": weather_summary,
    })


@app.route("/api/health")
def api_health():
    conn = get_db()
    pos_row = conn.execute("SELECT MAX(timestamp) as last_pos FROM positions").fetchone()
    weather_row = conn.execute("SELECT MAX(collected_at) as last_weather FROM weather_snapshots").fetchone()
    conn.close()
    return jsonify({
        "status": "ok", "server_time": datetime.now(timezone.utc).isoformat(),
        "last_ais_position": pos_row["last_pos"] if pos_row else None,
        "last_weather_collection": weather_row["last_weather"] if weather_row else None,
    })


# =============================================================================
# Tracker control endpoints
# =============================================================================

def _get_db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def _age_minutes(ts_str):
    """Return age in minutes from an ISO timestamp string, or None."""
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        return int(delta.total_seconds() / 60)
    except Exception:
        return None

@app.route("/api/tracker/status")
def api_tracker_status():
    # AIS service status
    result = subprocess.run(
        ['/usr/bin/systemctl', 'is-active', 'sailtracker-ais.service'],
        capture_output=True, text=True
    )
    ais_active = result.stdout.strip() == 'active'
    ais_status = result.stdout.strip()

    conn = _get_db_conn()
    # Last AIS position
    ais_row = conn.execute(
        "SELECT MAX(timestamp) as last_ts FROM positions WHERE source='ais'"
    ).fetchone()
    # Last InReach position
    inreach_row = conn.execute(
        "SELECT MAX(timestamp) as last_ts FROM positions WHERE source='inreach'"
    ).fetchone()
    # Total positions
    count_row = conn.execute("SELECT COUNT(*) as n FROM positions").fetchone()
    conn.close()

    ais_last_ts = ais_row['last_ts'] if ais_row else None
    inreach_last_ts = inreach_row['last_ts'] if inreach_row else None

    return jsonify({
        'ais': {'active': ais_active, 'status': ais_status},
        'ais_last': {'last_ts': ais_last_ts, 'age_minutes': _age_minutes(ais_last_ts)},
        'inreach': {'last_ts': inreach_last_ts, 'age_minutes': _age_minutes(inreach_last_ts)},
        'positions_count': count_row['n'] if count_row else 0,
    })

@app.route("/api/tracker/start", methods=["POST"])
def api_tracker_start():
    result = subprocess.run(
        ['/usr/bin/sudo', '/usr/bin/systemctl', 'start', 'sailtracker-ais.service'],
        capture_output=True, text=True
    )
    success = result.returncode == 0
    return jsonify({
        'success': success,
        'message': 'sailtracker-ais démarré' if success else f'Erreur : {result.stderr.strip()}'
    })

@app.route("/api/tracker/stop", methods=["POST"])
def api_tracker_stop():
    result = subprocess.run(
        ['/usr/bin/sudo', '/usr/bin/systemctl', 'stop', 'sailtracker-ais.service'],
        capture_output=True, text=True
    )
    success = result.returncode == 0
    return jsonify({
        'success': success,
        'message': 'sailtracker-ais arrêté' if success else f'Erreur : {result.stderr.strip()}'
    })

@app.route("/api/tracker/restart", methods=["POST"])
def api_tracker_restart():
    result = subprocess.run(
        ['/usr/bin/sudo', '/usr/bin/systemctl', 'restart', 'sailtracker-ais.service'],
        capture_output=True, text=True
    )
    success = result.returncode == 0
    return jsonify({
        'success': success,
        'message': 'sailtracker-ais redémarré' if success else f'Erreur : {result.stderr.strip()}'
    })

@app.route("/api/tracker/sync-inreach", methods=["POST"])
def api_tracker_sync_inreach():
    venv_python = str(BASE_DIR / 'venv' / 'bin' / 'python')
    collector = str(BASE_DIR / 'inreach_collector.py')
    subprocess.Popen(
        [venv_python, collector],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return jsonify({'success': True, 'message': 'Sync InReach lancé'})

@app.route("/api/tracker/reset", methods=["POST"])
def api_tracker_reset():
    import os, secrets as _secrets
    data = request.get_json(silent=True) or {}
    # Double protection : confirm + token secret
    if data.get('confirm') != 'RESET':
        return jsonify({'success': False, 'error': 'Confirmation requise (confirm: RESET)'}), 400
    admin_token = os.environ.get("SAILTRACKER_ADMIN_TOKEN", "")
    provided_token = data.get("token", "")
    if not admin_token:
        return jsonify({'success': False, 'error': 'SAILTRACKER_ADMIN_TOKEN non configuré côté serveur'}), 500
    if not _secrets.compare_digest(admin_token, provided_token):
        return jsonify({'success': False, 'error': 'Token invalide'}), 403
    conn = _get_db_conn()
    count_row = conn.execute("SELECT COUNT(*) as n FROM positions").fetchone()
    deleted = count_row['n'] if count_row else 0
    conn.execute("DELETE FROM positions")
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'deleted': deleted, 'message': f'{deleted} positions supprimées'})

# =============================================================================
# Init DB
# =============================================================================

def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
            speed_knots REAL, course REAL, heading REAL, nav_status TEXT,
            source TEXT DEFAULT 'ais', created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_positions_timestamp ON positions(timestamp DESC);

        CREATE TABLE IF NOT EXISTS weather_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
            wind_speed_kmh REAL, wind_direction_deg REAL, wind_gusts_kmh REAL,
            wave_height_m REAL, wave_direction_deg REAL, wave_period_s REAL,
            swell_height_m REAL, swell_direction_deg REAL, swell_period_s REAL,
            current_speed_knots REAL, current_direction_deg REAL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_weather_collected ON weather_snapshots(collected_at DESC);

        CREATE TABLE IF NOT EXISTS weather_forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at TEXT NOT NULL, forecast_time TEXT NOT NULL, data_type TEXT NOT NULL,
            value1 REAL, value2 REAL, value3 REAL, created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_forecast_time ON weather_forecasts(forecast_time);

        CREATE TABLE IF NOT EXISTS passage_routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            waypoints TEXT NOT NULL, boat_speed_avg_knots REAL DEFAULT 6.0,
            max_wind_knots REAL DEFAULT 30, max_wave_m REAL DEFAULT 3.0,
            max_swell_m REAL DEFAULT 3.5, status TEXT DEFAULT 'ready',
            last_computed TEXT, created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS passage_forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_id INTEGER NOT NULL, collected_at TEXT NOT NULL,
            waypoint_index INTEGER NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
            forecast_time TEXT NOT NULL, model TEXT NOT NULL,
            wind_speed_knots REAL, wind_direction_deg REAL, wind_gusts_knots REAL,
            wave_height_m REAL, wave_direction_deg REAL, wave_period_s REAL,
            swell_height_m REAL, swell_direction_deg REAL, swell_period_s REAL,
            FOREIGN KEY (route_id) REFERENCES passage_routes(id)
        );
        CREATE INDEX IF NOT EXISTS idx_passage_fc ON passage_forecasts(route_id,collected_at,model);

        CREATE TABLE IF NOT EXISTS departure_simulations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_id INTEGER NOT NULL, computed_at TEXT NOT NULL,
            departure_date TEXT NOT NULL, confidence_score REAL, comfort_score REAL,
            overall_score REAL, summary TEXT, alerts TEXT,
            FOREIGN KEY (route_id) REFERENCES passage_routes(id)
        );
        CREATE INDEX IF NOT EXISTS idx_dep_sim ON departure_simulations(route_id,computed_at);

        CREATE TABLE IF NOT EXISTS ensemble_forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at TEXT NOT NULL,
            route_id INTEGER NOT NULL,
            waypoint_index INTEGER NOT NULL,
            forecast_time TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT 'ecmwf_ens',
            member_id INTEGER NOT NULL,
            wind_speed_knots REAL,
            wind_direction_deg REAL,
            FOREIGN KEY (route_id) REFERENCES passage_routes(id)
        );
        CREATE INDEX IF NOT EXISTS idx_ens_query ON ensemble_forecasts(route_id, waypoint_index, collected_at);

        CREATE TABLE IF NOT EXISTS model_accuracy (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            model TEXT NOT NULL,
            zone TEXT NOT NULL,
            forecast_hour INTEGER NOT NULL,
            wind_speed_error_avg REAL,
            wind_dir_error_avg REAL,
            sample_count INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(date, model, zone, forecast_hour)
        );
        CREATE INDEX IF NOT EXISTS idx_acc_lookup ON model_accuracy(model, zone, forecast_hour, date);

        CREATE TABLE IF NOT EXISTS polar_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            latitude REAL, longitude REAL,
            sog_kts REAL, cog_deg REAL,
            tws_kts REAL, twd_deg REAL, twa_deg REAL,
            current_speed_kts REAL, current_dir_deg REAL,
            stw_kts REAL,
            is_valid INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_polar_obs ON polar_observations(timestamp DESC);

        CREATE TABLE IF NOT EXISTS route_optimizations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_id INTEGER NOT NULL,
            computed_at TEXT NOT NULL,
            departure TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_route_opt ON route_optimizations(route_id, computed_at DESC);
    """)
    # Migrations
    for col, definition in [("status", "TEXT DEFAULT 'ready'"), ("last_computed", "TEXT"), ("source", "TEXT DEFAULT 'ais'")]:
        try:
            c.execute(f"ALTER TABLE passage_routes ADD COLUMN {col} {definition}")
        except Exception:
            pass
    for col, definition in [("current_speed_knots", "REAL"), ("current_direction_deg", "REAL")]:
        try:
            c.execute(f"ALTER TABLE passage_forecasts ADD COLUMN {col} {definition}")
        except Exception:
            pass
    conn.commit()
    conn.close()

if __name__ == "__main__":
    logger.info("=== SailTracker Web Server démarré sur %s:%d ===", FLASK_HOST, FLASK_PORT)
    init_db()
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False)
