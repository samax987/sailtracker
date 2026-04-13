"""
blueprints/system.py — Routes système : health, stats, engine, tracker, at-sea, me.
"""
import json
import logging
import os
import secrets as _secrets
import subprocess
from datetime import datetime, timezone, timedelta

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user

from blueprints.shared import get_db, haversine_nm, minutes_ago, BASE_DIR

logger = logging.getLogger("sailtracker_server")

bp = Blueprint("system", __name__)


@bp.route("/api/health")
def api_health():
    conn = get_db()
    pos_row = conn.execute("SELECT MAX(timestamp) as last_pos FROM positions").fetchone()
    weather_row = conn.execute("SELECT MAX(collected_at) as last_weather FROM weather_snapshots").fetchone()
    conn.close()
    return jsonify({
        "status": "ok",
        "server_time": datetime.now(timezone.utc).isoformat(),
        "last_ais_position": pos_row["last_pos"] if pos_row else None,
        "last_weather_collection": weather_row["last_weather"] if weather_row else None,
    })


@bp.route("/api/stats")
@login_required
def api_stats():
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) as total, MIN(timestamp) as first_ts, MAX(timestamp) as last_ts, MAX(speed_knots) as max_speed FROM positions"
    ).fetchone()
    if not row or not row["total"]:
        conn.close()
        return jsonify({"error": "Aucune donnée"}), 404
    all_pos = conn.execute("SELECT latitude, longitude FROM positions ORDER BY timestamp ASC").fetchall()
    distance_nm = sum(
        haversine_nm(all_pos[i-1]["latitude"], all_pos[i-1]["longitude"],
                     all_pos[i]["latitude"], all_pos[i]["longitude"])
        for i in range(1, len(all_pos))
    )
    avg_row = conn.execute("SELECT AVG(speed_knots) FROM positions WHERE speed_knots > 0.5").fetchone()
    conn.close()
    return jsonify({
        "distance_nm": round(distance_nm, 1),
        "avg_speed_knots": round(avg_row[0] or 0, 1),
        "max_speed_knots": round(row["max_speed"] or 0, 1),
        "tracking_since": row["first_ts"],
        "last_update": row["last_ts"],
        "total_positions": row["total"],
    })


@bp.route("/api/engine/status")
@login_required
def api_engine_status():
    """Statut et benchmark du moteur de calcul Rust."""
    import time as _time
    # Import dynamique pour éviter les dépendances circulaires
    try:
        from rust_engine import engine_available, engine_state, rust_polar, rust_version
    except ImportError:
        def engine_available(): return False
        def engine_state(): return {"rust_binary_exists": False, "rust_binary_path": "", "last_rust_call": None, "last_rust_duration_ms": None, "last_python_fallback": None, "last_python_command": None}
        def rust_polar(twa, tws): return None
        def rust_version(): return None

    from polars import get_polar
    state = engine_state()
    ver = rust_version() if state["rust_binary_exists"] else None

    bench_rust_ms = None
    bench_python_ms = None
    if state["rust_binary_exists"]:
        try:
            t0 = _time.monotonic()
            rust_polar(90.0, 15.0)
            bench_rust_ms = round((_time.monotonic() - t0) * 1000, 1)
        except Exception:
            pass
    try:
        t0 = _time.monotonic()
        get_polar(user_id=current_user.id).get_boat_speed(90.0, 15.0)
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
        "benchmark": {"rust_ms": bench_rust_ms, "python_ms": bench_python_ms},
    })


@bp.route("/api/at-sea")
@login_required
def api_at_sea():
    """Détecte si le bateau est en navigation active sur une route connue."""
    conn = get_db()
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
    nearest_idx, min_wp_dist = 0, 999.0
    for i, wp in enumerate(wps):
        d = haversine_nm(lat, lon, wp["lat"], wp["lon"])
        if d < min_wp_dist:
            min_wp_dist = d
            nearest_idx = i

    total_dist = sum(haversine_nm(wps[i]["lat"], wps[i]["lon"], wps[i+1]["lat"], wps[i+1]["lon"]) for i in range(len(wps)-1))
    dist_covered = sum(haversine_nm(wps[i]["lat"], wps[i]["lon"], wps[i+1]["lat"], wps[i+1]["lon"]) for i in range(nearest_idx))
    progress_pct = round(dist_covered / total_dist * 100) if total_dist > 0 else 0
    dist_remaining = round(sum(haversine_nm(wps[i]["lat"], wps[i]["lon"], wps[i+1]["lat"], wps[i+1]["lon"]) for i in range(nearest_idx, len(wps)-1)) + min_wp_dist, 1)

    speeds_6h = conn.execute(
        "SELECT speed_knots FROM positions WHERE source='inreach' AND speed_knots > 0 AND timestamp >= datetime('now','-6 hours')"
    ).fetchall()
    avg_speed = (sum(r["speed_knots"] for r in speeds_6h) / len(speeds_6h)) if speeds_6h else speed

    eta_str, hours_remaining = None, None
    if avg_speed > 0:
        hours_remaining = round(dist_remaining / avg_speed, 1)
        eta_str = (datetime.now(timezone.utc) + timedelta(hours=hours_remaining)).strftime("%d/%m %Hh%M UTC")

    wx = conn.execute(
        "SELECT wind_speed_kmh, wind_direction_deg, wave_height_m FROM weather_snapshots ORDER BY collected_at DESC LIMIT 1"
    ).fetchone()
    weather_summary = None
    if wx:
        weather_summary = {
            "wind_knots": round((wx["wind_speed_kmh"] or 0) / 1.852, 1),
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


@bp.route("/api/me")
@login_required
def api_me():
    return jsonify({
        "username": current_user.username,
        "boat_name": current_user.boat_name,
        "boat_type": current_user.boat_type,
        "is_admin": current_user.is_admin,
    })


# =============================================================================
# Tracker control
# =============================================================================

@bp.route("/api/tracker/status")
def api_tracker_status():
    result = subprocess.run(["/usr/bin/systemctl", "is-active", "sailtracker-ais.service"], capture_output=True, text=True)
    ais_active = result.stdout.strip() == "active"
    conn = get_db()
    ais_row = conn.execute("SELECT MAX(timestamp) as last_ts FROM positions WHERE source='ais'").fetchone()
    inreach_row = conn.execute("SELECT MAX(timestamp) as last_ts FROM positions WHERE source='inreach'").fetchone()
    count_row = conn.execute("SELECT COUNT(*) as n FROM positions").fetchone()
    conn.close()
    ais_last_ts = ais_row["last_ts"] if ais_row else None
    inreach_last_ts = inreach_row["last_ts"] if inreach_row else None
    return jsonify({
        "ais": {"active": ais_active, "status": result.stdout.strip()},
        "ais_last": {"last_ts": ais_last_ts, "age_minutes": minutes_ago(ais_last_ts)},
        "inreach": {"last_ts": inreach_last_ts, "age_minutes": minutes_ago(inreach_last_ts)},
        "positions_count": count_row["n"] if count_row else 0,
    })


@bp.route("/api/tracker/start", methods=["POST"])
def api_tracker_start():
    result = subprocess.run(["/usr/bin/sudo", "/usr/bin/systemctl", "start", "sailtracker-ais.service"], capture_output=True, text=True)
    success = result.returncode == 0
    return jsonify({"success": success, "message": "sailtracker-ais démarré" if success else f"Erreur : {result.stderr.strip()}"})


@bp.route("/api/tracker/stop", methods=["POST"])
def api_tracker_stop():
    result = subprocess.run(["/usr/bin/sudo", "/usr/bin/systemctl", "stop", "sailtracker-ais.service"], capture_output=True, text=True)
    success = result.returncode == 0
    return jsonify({"success": success, "message": "sailtracker-ais arrêté" if success else f"Erreur : {result.stderr.strip()}"})


@bp.route("/api/tracker/restart", methods=["POST"])
def api_tracker_restart():
    result = subprocess.run(["/usr/bin/sudo", "/usr/bin/systemctl", "restart", "sailtracker-ais.service"], capture_output=True, text=True)
    success = result.returncode == 0
    return jsonify({"success": success, "message": "sailtracker-ais redémarré" if success else f"Erreur : {result.stderr.strip()}"})


@bp.route("/api/tracker/sync-inreach", methods=["POST"])
def api_tracker_sync_inreach():
    venv_python = str(BASE_DIR / "venv" / "bin" / "python")
    collector = str(BASE_DIR / "inreach_collector.py")
    subprocess.Popen([venv_python, collector], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return jsonify({"success": True, "message": "Sync InReach lancé"})


@bp.route("/api/tracker/reset", methods=["POST"])
def api_tracker_reset():
    data = request.get_json(silent=True) or {}
    if data.get("confirm") != "RESET":
        return jsonify({"success": False, "error": "Confirmation requise (confirm: RESET)"}), 400
    admin_token = os.environ.get("SAILTRACKER_ADMIN_TOKEN", "")
    if not admin_token:
        return jsonify({"success": False, "error": "SAILTRACKER_ADMIN_TOKEN non configuré"}), 500
    if not _secrets.compare_digest(admin_token, data.get("token", "")):
        logger.warning("Tentative token admin invalide depuis %s", request.remote_addr)
        return jsonify({"success": False, "error": "Token invalide"}), 403
    conn = get_db()
    count_row = conn.execute("SELECT COUNT(*) as n FROM positions").fetchone()
    deleted = count_row["n"] if count_row else 0
    conn.execute("DELETE FROM positions")
    conn.commit()
    conn.close()
    return jsonify({"success": True, "deleted": deleted, "message": f"{deleted} positions supprimées"})
