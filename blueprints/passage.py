import concurrent.futures
import gzip
import json
import logging
import math
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from flask import Blueprint, jsonify, make_response, render_template, request
from flask_login import current_user, login_required

from .shared import (
    BASE_DIR, GRIB_CACHE_DIR, get_db, get_route_owned, great_circle_waypoints, haversine_nm
)

bp = Blueprint("passage", __name__)
logger = logging.getLogger("sailtracker_server")

# État partagé pour les tâches de routage isochrones
# Tasks de routing : persistées en SQLite pour être visibles depuis tous les workers
# gunicorn (le dict mémoire ne fonctionnait qu'avec 1 worker, sinon 404 aléatoire).
_TASKS_TABLE_INITIALIZED = False
_routing_tasks_lock = threading.Lock()  # lock local pour la création de table


def _ensure_routing_tasks_table(conn):
    global _TASKS_TABLE_INITIALIZED
    if _TASKS_TABLE_INITIALIZED:
        return
    conn.execute(
        """CREATE TABLE IF NOT EXISTS routing_tasks (
            task_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            progress INTEGER DEFAULT 0,
            error TEXT,
            result_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    # Purge des tasks de plus de 1h pour éviter que la table grossisse indéfiniment
    conn.execute(
        "DELETE FROM routing_tasks WHERE created_at < datetime('now', '-1 hour')"
    )
    conn.commit()
    _TASKS_TABLE_INITIALIZED = True


def _routing_task_create(task_id: str) -> None:
    conn = get_db()
    try:
        _ensure_routing_tasks_table(conn)
        conn.execute(
            "INSERT INTO routing_tasks (task_id, status, progress) VALUES (?, 'computing', 0)",
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _routing_task_set_done(task_id: str, result: dict) -> None:
    conn = get_db()
    try:
        _ensure_routing_tasks_table(conn)
        conn.execute(
            "UPDATE routing_tasks SET status='done', progress=100, result_json=?, updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
            (json.dumps(result), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def _routing_task_set_error(task_id: str, err: str) -> None:
    conn = get_db()
    try:
        _ensure_routing_tasks_table(conn)
        conn.execute(
            "UPDATE routing_tasks SET status='error', error=?, updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
            (err, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def _routing_task_get(task_id: str):
    conn = get_db()
    try:
        _ensure_routing_tasks_table(conn)
        row = conn.execute(
            "SELECT status, progress, error, result_json FROM routing_tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    return row

# Instance GribWindProvider (chargée à la demande)
_wind_provider = None
_wind_provider_lock = threading.Lock()

_DATETIME_RE = re.compile(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}')


def _get_wind_provider():
    global _wind_provider
    with _wind_provider_lock:
        if _wind_provider is None:
            from routing import GribWindProvider
            _wind_provider = GribWindProvider()
        return _wind_provider


def _parse_user_datetime(s):
    """Valide et parse une chaîne datetime saisie par l'utilisateur."""
    if not s or not _DATETIME_RE.match(s):
        raise ValueError(f"Format invalide: {s!r}")
    return datetime.fromisoformat(s.replace(' ', 'T'))


# =============================================================================
# API : Gestion des routes de passage
# =============================================================================

@bp.route("/api/routes", methods=["GET"])
@login_required
def api_routes_list():
    conn = get_db()
    rows = conn.execute(
        "SELECT id,name,boat_speed_avg_knots,max_wind_knots,max_wave_m,max_swell_m,created_at,status,last_computed,phase,actual_departure,actual_arrival,departure_port,arrival_port FROM passage_routes WHERE user_id=? ORDER BY id",
        (current_user.id,)
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
        "phase": r["phase"] or "planning",
        "actual_departure": r["actual_departure"],
        "actual_arrival": r["actual_arrival"],
        "departure_port": r["departure_port"],
        "arrival_port": r["arrival_port"],
    } for r in rows]})


@bp.route("/api/routes", methods=["POST"])
@login_required
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
        "INSERT INTO passage_routes (user_id,name,waypoints,boat_speed_avg_knots,max_wind_knots,max_wave_m,max_swell_m,status) VALUES (?,?,?,?,?,?,?,'pending')",
        (current_user.id, name, json.dumps(waypoints, ensure_ascii=False), boat_speed, max_wind, max_wave, max_swell),
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


@bp.route("/api/gpx/parse", methods=["POST"])
@login_required
def api_gpx_parse():
    try:
        import defusedxml.ElementTree as ET
    except ImportError:
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
            if len(raw_bytes) > 10 * 1024 * 1024:
                return jsonify({"error": "Fichier trop volumineux (max 10 MB)"}), 413
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


# =============================================================================
# API : Info / calcul / prévisions de traversée
# =============================================================================

@bp.route("/api/passage/<int:route_id>/info")
@login_required
def api_passage_info(route_id):
    conn = get_db()
    row = get_route_owned(conn, route_id, current_user.id)
    if row is None:
        conn.close()
        return jsonify({"error": "Route non trouvée"}), 404
    waypoints = json.loads(row["waypoints"])
    total_nm = sum(haversine_nm(waypoints[i-1]["lat"], waypoints[i-1]["lon"],
                                waypoints[i]["lat"], waypoints[i]["lon"])
                   for i in range(1, len(waypoints)))
    speed_fallback = row["boat_speed_avg_knots"] or 6.0

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
            s = json.loads(best_sim["summary"])
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
        "phase": row["phase"] or "planning",
        "actual_departure": row["actual_departure"],
        "actual_arrival": row["actual_arrival"],
        "departure_port": row["departure_port"],
        "arrival_port": row["arrival_port"],
    })


@bp.route("/api/passage/<int:route_id>/compute", methods=["POST"])
@login_required
def api_compute_passage(route_id):
    conn = get_db()
    row = get_route_owned(conn, route_id, current_user.id, "id,status")
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


@bp.route("/api/passage/<int:route_id>/compute_status")
@login_required
def api_compute_status(route_id):
    conn = get_db()
    row = get_route_owned(conn, route_id, current_user.id, "status,last_computed")
    conn.close()
    if not row:
        return jsonify({"error": "Route non trouvée"}), 404
    return jsonify({
        "route_id": route_id,
        "status": row["status"] or "ready",
        "last_computed": row["last_computed"],
    })


@bp.route("/api/passage/<int:route_id>/forecast")
@login_required
def api_passage_forecast(route_id):
    conn = get_db()
    if not get_route_owned(conn, route_id, current_user.id, "id"):
        conn.close()
        return jsonify({"error": "Route non trouvée"}), 404
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


@bp.route("/api/passage/<int:route_id>/departures")
@login_required
def api_passage_departures(route_id):
    conn = get_db()
    if not get_route_owned(conn, route_id, current_user.id, "id"):
        conn.close()
        return jsonify({"error": "Route non trouvée"}), 404
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
            except (json.JSONDecodeError, TypeError): alerts = [r["alerts"]] if r["alerts"] else []
        overall = r["overall_score"] or 0
        verdict = "GO" if overall >= 70 else ("ATTENTION" if overall >= 45 else "NO-GO")
        summary_data = {}
        if r["summary"]:
            try: summary_data = json.loads(r["summary"])
            except (json.JSONDecodeError, TypeError): pass
        simulations.append({
            "departure_date": r["departure_date"],
            "confidence_score": r["confidence_score"],
            "comfort_score": r["comfort_score"],
            "overall_score": overall, "alerts": alerts, "verdict": verdict,
            "current_effect_knots": summary_data.get("current_effect_knots"),
            "adjusted_eta_hours": summary_data.get("adjusted_eta_hours"),
        })
    return jsonify({"route_id": route_id, "computed_at": computed_at, "simulations": simulations})


@bp.route("/api/passage/<int:route_id>/ensemble")
@login_required
def api_passage_ensemble(route_id):
    wp_idx = int(request.args.get('wp', 0))
    conn = get_db()
    if not get_route_owned(conn, route_id, current_user.id, "id"):
        conn.close()
        return jsonify({"error": "Route non trouvée"}), 404

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
# API : Phases de traversée (planning / active / completed)
# =============================================================================

@bp.route("/api/passage/<int:route_id>/start", methods=["POST"])
@login_required
def api_passage_start(route_id):
    """Démarre une traversée : phase planning → active. Filtré par user."""
    data = request.get_json() or {}
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, phase FROM passage_routes WHERE id=? AND user_id=?",
            (route_id, current_user.id)
        ).fetchone()
        if not row:
            return jsonify({"error": "Route non trouvée"}), 404
        if row["phase"] != "planning":
            return jsonify({"error": f"La route est déjà en phase '{row['phase']}'"}), 409
        raw = data.get("actual_departure", "")
        try:
            departure_time = _parse_user_datetime(raw).strftime("%Y-%m-%d %H:%M:%S") if raw else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return jsonify({"error": "Format de date invalide (attendu: YYYY-MM-DD HH:MM)"}), 400
        conn.execute(
            "UPDATE passage_routes SET phase='active', actual_departure=? WHERE id=?",
            (departure_time, route_id)
        )
        conn.commit()
        return jsonify({"status": "active", "actual_departure": departure_time})
    finally:
        conn.close()


@bp.route("/api/passage/<int:route_id>/arrive", methods=["POST"])
@login_required
def api_passage_arrive(route_id):
    """Enregistre l'arrivée : phase active → completed. Filtré par user."""
    data = request.get_json() or {}
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, phase, actual_departure FROM passage_routes WHERE id=? AND user_id=?",
            (route_id, current_user.id)
        ).fetchone()
        if not row:
            return jsonify({"error": "Route non trouvée"}), 404
        if row["phase"] != "active":
            return jsonify({"error": f"La traversée n'est pas active (phase: '{row['phase']}')"}), 409
        raw = data.get("actual_arrival", "")
        try:
            arrival_time = _parse_user_datetime(raw).strftime("%Y-%m-%d %H:%M:%S") if raw else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return jsonify({"error": "Format de date invalide (attendu: YYYY-MM-DD HH:MM)"}), 400
        notes = data.get("notes", "")[:2000]

        duration_h = None
        if row["actual_departure"]:
            try:
                dep = _parse_user_datetime(row["actual_departure"])
                arr = _parse_user_datetime(arrival_time)
                if arr <= dep:
                    return jsonify({"error": "La date d'arrivée doit être postérieure au départ"}), 400
                duration_h = round((arr - dep).total_seconds() / 3600, 1)
            except ValueError:
                pass

        conn.execute(
            "UPDATE passage_routes SET phase='completed', actual_arrival=?, notes=? WHERE id=?",
            (arrival_time, notes, route_id)
        )
        conn.commit()
        return jsonify({"status": "completed", "actual_arrival": arrival_time, "duration_hours": duration_h})
    finally:
        conn.close()


@bp.route("/api/passage/<int:route_id>/active-weather")
@login_required
def api_passage_active_weather(route_id):
    """Météo aux prochains waypoints pour une traversée active."""
    conn = get_db()
    try:
        route_row = get_route_owned(conn, route_id, current_user.id)
        if not route_row:
            return jsonify({"error": "Route non trouvée"}), 404

        waypoints = json.loads(route_row["waypoints"])

        pos_row = conn.execute(
            "SELECT latitude, longitude, timestamp FROM positions ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        boat_lat = pos_row["latitude"] if pos_row else waypoints[0]["lat"]
        boat_lon = pos_row["longitude"] if pos_row else waypoints[0]["lon"]
        boat_ts = pos_row["timestamp"] if pos_row else None

        nearest_idx = min(range(len(waypoints)),
                          key=lambda i: haversine_nm(boat_lat, boat_lon,
                                                     waypoints[i]["lat"], waypoints[i]["lon"]))

        next_start = max(0, nearest_idx)
        next_wps_idx = list(range(next_start, min(len(waypoints), next_start + 5)))

        last_collected = conn.execute(
            "SELECT MAX(collected_at) as last FROM passage_forecasts WHERE route_id=?", (route_id,)
        ).fetchone()

        weather_by_wp = {}
        if last_collected and last_collected["last"]:
            collected_at = last_collected["last"]
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")

            for wp_idx in next_wps_idx:
                rows = conn.execute(
                    """SELECT forecast_time, wind_speed_knots, wind_direction_deg, wind_gusts_knots,
                              wave_height_m, swell_height_m, swell_period_s, current_speed_knots
                       FROM passage_forecasts
                       WHERE route_id=? AND collected_at=? AND waypoint_index=?
                         AND forecast_time >= ?
                       ORDER BY forecast_time ASC LIMIT 16""",
                    (route_id, collected_at, wp_idx, now_str)
                ).fetchall()

                if rows:
                    wp = waypoints[wp_idx]
                    dist_nm = haversine_nm(boat_lat, boat_lon, wp["lat"], wp["lon"])
                    weather_by_wp[wp_idx] = {
                        "waypoint": {
                            "index": wp_idx,
                            "name": wp.get("name", f"WP{wp_idx+1}"),
                            "lat": wp["lat"],
                            "lon": wp["lon"],
                            "distance_nm": round(dist_nm, 0),
                        },
                        "forecasts": [{
                            "time": r["forecast_time"],
                            "wind_knots": r["wind_speed_knots"],
                            "wind_dir": r["wind_direction_deg"],
                            "gusts_knots": r["wind_gusts_knots"],
                            "wave_m": r["wave_height_m"],
                            "swell_m": r["swell_height_m"],
                            "current_kn": r["current_speed_knots"],
                        } for r in rows],
                    }

        return jsonify({
            "route_id": route_id,
            "boat_position": {"lat": boat_lat, "lon": boat_lon, "timestamp": boat_ts},
            "nearest_waypoint_index": nearest_idx,
            "next_waypoints": [weather_by_wp[i] for i in next_wps_idx if i in weather_by_wp],
            "collected_at": last_collected["last"] if last_collected else None,
        })
    finally:
        conn.close()


@bp.route("/api/passage/<int:route_id>/completed-summary")
@login_required
def api_passage_completed_summary(route_id):
    """Bilan d'une traversée complétée."""
    conn = get_db()
    try:
        row = get_route_owned(conn, route_id, current_user.id)
        if not row:
            return jsonify({"error": "Route non trouvée"}), 404

        waypoints = json.loads(row["waypoints"])
        total_nm = sum(
            haversine_nm(waypoints[i-1]["lat"], waypoints[i-1]["lon"],
                         waypoints[i]["lat"], waypoints[i]["lon"])
            for i in range(1, len(waypoints))
        )

        duration_h = None
        avg_speed_kn = None
        if row["actual_departure"] and row["actual_arrival"]:
            try:
                dep = datetime.fromisoformat(row["actual_departure"])
                arr = datetime.fromisoformat(row["actual_arrival"])
                duration_h = (arr - dep).total_seconds() / 3600
                avg_speed_kn = round(total_nm / duration_h, 2) if duration_h > 0 else None
            except ValueError:
                pass

        return jsonify({
            "route_id": route_id,
            "name": row["name"],
            "departure_port": row["departure_port"],
            "arrival_port": row["arrival_port"],
            "actual_departure": row["actual_departure"],
            "actual_arrival": row["actual_arrival"],
            "total_distance_nm": round(total_nm, 0),
            "duration_hours": round(duration_h, 1) if duration_h else None,
            "duration_days": round(duration_h / 24, 1) if duration_h else None,
            "avg_speed_knots": avg_speed_kn,
            "notes": row["notes"],
            "waypoints_count": len(waypoints),
        })
    finally:
        conn.close()




# =============================================================================
# API : Estimation d'une route manuelle (analyse segment par segment)
# =============================================================================

@bp.route("/api/passage/routes/<int:route_id>/analyze", methods=["POST"])
@login_required
def api_analyze_route(route_id):
    """Analyse les waypoints saisis : TWA, allure, vitesse polaire, ETA, warnings.
    Pas d'optimisation — utilise EXACTEMENT les waypoints fournis par l'utilisateur."""
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
    route = get_route_owned(conn, route_id, current_user.id)
    conn.close()
    if not route:
        return jsonify({"error": "Route introuvable"}), 404

    try:
        waypoints = json.loads(route["waypoints"])
    except Exception:
        return jsonify({"error": "Waypoints invalides"}), 400

    if len(waypoints) < 2:
        return jsonify({"error": "La route doit avoir au moins 2 waypoints"}), 400

    from polars import get_polar
    from routing import analyze_route
    polar = get_polar(str(BASE_DIR / "sailtracker.db"))
    wind_prov = _get_wind_provider()

    try:
        result = analyze_route(waypoints, departure_dt, polar, wind_prov)
    except Exception as e:
        return jsonify({"error": f"Analyse échouée : {e}"}), 500

    return jsonify(result)

# =============================================================================
# API : Routage isochrones
# =============================================================================

@bp.route("/api/passage/routes/<int:route_id>/optimize", methods=["POST"])
@login_required
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
    route = get_route_owned(conn, route_id, current_user.id)
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
    _routing_task_create(task_id)

    def _do_routing():
        from polars import get_polar
        from routing import isochrone_routing
        polar = get_polar(str(BASE_DIR / "sailtracker.db"))
        wind_prov = _get_wind_provider()
        s = (waypoints[0]["lat"], waypoints[0]["lon"])
        e = (waypoints[-1]["lat"], waypoints[-1]["lon"])
        return isochrone_routing(s, e, departure_dt, polar, wind_prov)

    def run_routing():
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_do_routing)
                try:
                    result = future.result(timeout=90)
                except concurrent.futures.TimeoutError:
                    future.cancel()
                    raise RuntimeError("Calcul interrompu apres 90s")
            db2 = get_db()
            db2.execute(
                "INSERT INTO route_optimizations (route_id, computed_at, departure, result_json) VALUES (?, datetime('now'), ?, ?)",
                (route_id, departure_str, json.dumps(result))
            )
            db2.commit()
            db2.close()
            _routing_task_set_done(task_id, result)
        except Exception as ex:
            logger.error("Erreur routage tache %s: %s", task_id, ex)
            _routing_task_set_error(task_id, str(ex))

    threading.Thread(target=run_routing, daemon=True).start()
    return jsonify({"task_id": task_id, "status": "computing"})


@bp.route("/api/passage/routes/<int:route_id>/optimize/status")
@login_required
def api_optimize_status(route_id):
    task_id = request.args.get("task_id", "")
    row = _routing_task_get(task_id)
    if not row:
        return jsonify({"error": "Tâche inconnue"}), 404
    return jsonify({"status": row["status"], "progress": row["progress"], "error": row["error"]})


@bp.route("/api/passage/routes/<int:route_id>/optimize/result")
@login_required
def api_optimize_result(route_id):
    task_id = request.args.get("task_id", "")
    row = _routing_task_get(task_id)
    if not row:
        return jsonify({"error": "Tâche inconnue"}), 404
    if row["status"] != "done":
        return jsonify({"error": f"Calcul en cours ({row['status']})"}), 202
    return jsonify(json.loads(row["result_json"]))


@bp.route("/api/passage/routes/<int:route_id>/move-waypoint", methods=["POST"])
@login_required
def api_move_waypoint(route_id):
    """Met à jour la position d'un waypoint (index dans le tableau JSON)."""
    data = request.get_json() or {}
    idx = data.get("index")
    lat = data.get("lat")
    lon = data.get("lon")
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
        row = get_route_owned(conn, route_id, current_user.id, "waypoints")
        if not row:
            return jsonify({"success": False, "error": "Route non trouvée"}), 404
        wps = json.loads(row["waypoints"])
        if idx < 0 or idx >= len(wps):
            return jsonify({"success": False, "error": f"Index {idx} hors limites"}), 400
        wps[idx]["lat"] = round(lat, 6)
        wps[idx]["lon"] = round(lon, 6)
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


@bp.route("/api/passage/routes/<int:route_id>/rename", methods=["POST"])
@login_required
def api_rename_route(route_id):
    data = request.get_json() or {}
    new_name = data.get("name", "").strip()
    if not new_name:
        return jsonify({"success": False, "error": "Le nom ne peut pas être vide"}), 400
    if len(new_name) > 100:
        return jsonify({"success": False, "error": "Nom trop long (max 100 caractères)"}), 400
    conn = get_db()
    try:
        row = get_route_owned(conn, route_id, current_user.id, "id")
        if not row:
            return jsonify({"success": False, "error": "Route non trouvée"}), 404
        conn.execute("UPDATE passage_routes SET name=? WHERE id=?", (new_name, route_id))
        conn.commit()
        return jsonify({"success": True, "name": new_name})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        conn.close()


@bp.route("/api/passage/routes/<int:route_id>/delete", methods=["POST"])
@login_required
def api_delete_route(route_id):
    conn = get_db()
    try:
        row = get_route_owned(conn, route_id, current_user.id, "id, name")
        if not row:
            return jsonify({"success": False, "error": "Route non trouvée"}), 404
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
# API : Vent GRIB (wind-grid)
# =============================================================================

@bp.route("/api/passage/wind-grid")
@login_required
def api_passage_wind_grid():
    """Retourne une grille de vecteurs vent depuis les fichiers GRIB."""
    run_param = request.args.get('run')
    fh_param = request.args.get('fh', 'f000')
    route_id_param = request.args.get('route_id')

    index_file = GRIB_CACHE_DIR / "index.json"
    if not index_file.exists():
        return jsonify({"error": "Données GRIB non disponibles", "grid": []}), 200
    try:
        with open(index_file) as f:
            idx = json.load(f)
    except Exception as e:
        return jsonify({"error": str(e), "grid": []}), 500

    runs = idx.get('runs', [])
    if not runs:
        return jsonify({"error": "Aucun run disponible", "grid": []}), 200

    available_times = []
    for r in runs:
        run_id = r['run']
        fh_labels = r.get('fh_labels', [])
        valid_times = r.get('valid_times', [])
        for i, fh in enumerate(fh_labels):
            vt = valid_times[i] if i < len(valid_times) else ''
            available_times.append({'run': run_id, 'fh': fh, 'valid_time': vt})

    selected_run = run_param or runs[-1]['run']
    run_info = next((r for r in runs if r['run'] == selected_run), runs[-1])

    fh_labels = run_info.get('fh_labels', ['f000'])
    if fh_param not in fh_labels:
        fh_param = fh_labels[0]

    fh_idx = fh_labels.index(fh_param)
    valid_times = run_info.get('valid_times', [])
    forecast_time = valid_times[fh_idx] if fh_idx < len(valid_times) else ''

    wind_file = GRIB_CACHE_DIR / f"wind_{selected_run}_{fh_param}.json"
    if not wind_file.exists():
        return jsonify({"error": f"Fichier GRIB non trouvé: {wind_file.name}", "grid": []}), 200
    try:
        with open(wind_file) as f:
            grib_data = json.load(f)
    except Exception as e:
        return jsonify({"error": str(e), "grid": []}), 500

    u_entry = next((e for e in grib_data if e['header'].get('parameterNumber') == 2), None)
    v_entry = next((e for e in grib_data if e['header'].get('parameterNumber') == 3), None)
    if not u_entry or not v_entry:
        return jsonify({"error": "Composantes U/V non trouvées", "grid": []}), 200

    hdr = u_entry['header']
    la1 = hdr['la1']
    lo1 = hdr['lo1']
    la2 = hdr['la2']
    lo2 = hdr['lo2']
    dx = hdr['dx']
    dy = hdr['dy']
    nx = hdr['nx']
    ny = hdr['ny']
    u_data = u_entry['data']
    v_data = v_entry['data']

    lat_min_box, lat_max_box = 8.0, 22.0
    lon_min_box, lon_max_box = -68.0, -18.0

    if route_id_param:
        try:
            conn = get_db()
            row = get_route_owned(conn, int(route_id_param), current_user.id, "waypoints")
            conn.close()
            if row:
                wps = json.loads(row['waypoints'])
                lats = [w['lat'] for w in wps]
                lons = [w['lon'] for w in wps]
                lat_min_box = min(lats) - 3
                lat_max_box = max(lats) + 3
                lon_min_box = min(lons) - 3
                lon_max_box = max(lons) + 3
        except Exception as e:
            logger.warning("wind-grid: impossible de lire la route %s: %s", route_id_param, e)

    lat_min_box = max(lat_min_box, la2)
    lat_max_box = min(lat_max_box, la1)
    lon_min_box = max(lon_min_box, lo1)
    lon_max_box = min(lon_max_box, lo2)

    if route_id_param:
        try:
            _c = get_db()
            _row = get_route_owned(_c, int(route_id_param), current_user.id, "waypoints")
            _c.close()
            wps_for_dist = json.loads(_row["waypoints"]) if _row else []
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
        SKIP = 2
    elif route_dist_nm < 500:
        SKIP = 4
    else:
        SKIP = 8

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
# API : Briefing météo passage
# =============================================================================

@bp.route("/api/passage/<int:route_id>/briefing")
@login_required
def api_passage_briefing(route_id):
    """Génère un briefing météo en langage marin pour la route."""
    from briefing import generate_weather_briefing, bearing as _bearing

    conn = get_db()
    try:
        route_row = get_route_owned(conn, route_id, current_user.id, "waypoints, boat_speed_avg_knots")
        if not route_row:
            return jsonify({"error": "Route non trouvée"}), 404

        waypoints = json.loads(route_row['waypoints'])
        boat_speed = route_row['boat_speed_avg_knots'] or 6.0

        best_dep = conn.execute(
            "SELECT departure_date, overall_score FROM departure_simulations WHERE route_id=? ORDER BY overall_score DESC LIMIT 1",
            (route_id,)
        ).fetchone()
        best_departure_date = best_dep['departure_date'] if best_dep else None
        best_score = best_dep['overall_score'] if best_dep else None

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

    total_nm = 0.0
    nm_cumul = []
    for i, wp in enumerate(waypoints):
        if i == 0:
            nm_cumul.append(0.0)
        else:
            prev = waypoints[i - 1]
            total_nm += haversine_nm(prev['lat'], prev['lon'], wp['lat'], wp['lon'])
            nm_cumul.append(total_nm)

    if len(waypoints) >= 2:
        route_bearing = _bearing(waypoints[0]['lat'], waypoints[0]['lon'], waypoints[-1]['lat'], waypoints[-1]['lon'])
    else:
        route_bearing = 0.0

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
# API : Passage summary (page lite)
# =============================================================================

def build_passage_summary(user_id=None):
    """Construit le résumé de passage pour la page lite, filtré par user."""
    conn = get_db()
    try:
        if user_id is None:
            return None
        # Priorise une traversée active, sinon dernière route 'ready' planning du user
        route_row = conn.execute(
            """SELECT id, name, boat_speed_avg_knots FROM passage_routes
               WHERE user_id=? AND status='ready' AND COALESCE(status,'')<>'archived'
                 AND phase IN ('planning','active')
               ORDER BY (phase='active') DESC, id DESC LIMIT 1""",
            (user_id,)
        ).fetchone()
        if not route_row:
            return None

        route_id = route_row["id"]
        route_name = route_row["name"]
        boat_speed = route_row["boat_speed_avg_knots"]

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

        route_info_row = conn.execute("SELECT waypoints FROM passage_routes WHERE id=?", (route_id,)).fetchone()
        waypoints = json.loads(route_info_row["waypoints"]) if route_info_row else []

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

            total_nm = sum(haversine_nm(waypoints[i-1]["lat"], waypoints[i-1]["lon"],
                                        waypoints[i]["lat"], waypoints[i]["lon"])
                          for i in range(1, len(waypoints)))
            segment_size = max(1, len(waypoints) // 4)
            for seg_i in range(0, len(waypoints)-1, segment_size):
                seg_wps = list(range(seg_i, min(seg_i + segment_size, len(waypoints)-1)))
                seg_winds, seg_waves, seg_currents = [], [], []
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

        departures = []
        best_score = -1
        best_dep = None
        for r in sim_rows:
            overall = r["overall_score"] or 0
            alerts = []
            if r["alerts"]:
                try: alerts = json.loads(r["alerts"])[:2]
                except (json.JSONDecodeError, TypeError): pass
            summary_d = {}
            if r["summary"]:
                try: summary_d = json.loads(r["summary"])
                except (json.JSONDecodeError, TypeError): pass
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

        acc_rows = conn.execute(
            "SELECT model, AVG(wind_speed_error_avg) as avg_err, COUNT(*) as n FROM model_accuracy WHERE date >= date('now','-30 days') GROUP BY model ORDER BY avg_err ASC LIMIT 1"
        ).fetchall()
        best_model = None
        if acc_rows:
            r = acc_rows[0]
            best_model = {"model": r["model"], "error_kts": round(r["avg_err"], 1), "n_days": r["n"]}

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


@bp.route("/api/passage/summary")
@login_required
def api_passage_summary():
    data = build_passage_summary(user_id=current_user.id)
    if not data:
        return jsonify({"error": "Pas de données disponibles"}), 404
    resp_data = json.dumps(data, ensure_ascii=False)
    response = make_response(gzip.compress(resp_data.encode('utf-8')))
    response.headers['Content-Encoding'] = 'gzip'
    response.headers['Content-Type'] = 'application/json'
    return response


@bp.route("/passage/lite")
@login_required
def passage_lite():
    data = build_passage_summary(user_id=current_user.id)
    return render_template('passage_lite.html', data=data)
