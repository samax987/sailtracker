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

from blueprints.shared import get_db, haversine_nm, minutes_ago, BASE_DIR, compute_at_sea_status

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
    uid = current_user.id
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) as total, MIN(timestamp) as first_ts, MAX(timestamp) as last_ts, MAX(speed_knots) as max_speed FROM positions WHERE user_id=?",
        (uid,)
    ).fetchone()
    if not row or not row["total"]:
        conn.close()
        return jsonify({"error": "Aucune donnée"}), 404
    all_pos = conn.execute("SELECT latitude, longitude FROM positions WHERE user_id=? ORDER BY timestamp ASC", (uid,)).fetchall()
    distance_nm = sum(
        haversine_nm(all_pos[i-1]["latitude"], all_pos[i-1]["longitude"],
                     all_pos[i]["latitude"], all_pos[i]["longitude"])
        for i in range(1, len(all_pos))
    )
    avg_row = conn.execute("SELECT AVG(speed_knots) FROM positions WHERE user_id=? AND speed_knots > 0.5", (uid,)).fetchone()
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
    try:
        status = compute_at_sea_status(conn, current_user.id)
        if not status:
            # Diagnostic minimal pour rétrocompatibilité (pas de raison détaillée)
            return jsonify({"at_sea": False, "reason": "Bateau au mouillage ou hors route"})

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
        return jsonify({"at_sea": True, **status, "weather": weather_summary})
    finally:
        conn.close()


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


# =============================================================================
# API : mode navigation / mouillage
# =============================================================================

@bp.route("/api/mode", methods=["GET"])
@login_required
def api_mode_get():
    """Retourne le mode actuel (sailing ou anchor)."""
    mode = os.environ.get("INREACH_MODE", "sailing").lower()
    return jsonify({"mode": mode})


@bp.route("/api/mode", methods=["POST"])
@login_required
def api_mode_set():
    """Bascule entre sailing et anchor — met a jour .env."""
    import re as _re
    from pathlib import Path as _Path
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "").lower()
    if mode not in ("sailing", "anchor"):
        return jsonify({"error": "mode doit etre sailing ou anchor"}), 400

    env_path = _Path(__file__).parent.parent / ".env"
    try:
        text = env_path.read_text()
        if "INREACH_MODE=" in text:
            text = _re.sub(r"INREACH_MODE=\S*", f"INREACH_MODE={mode}", text)
        else:
            text += f"\nINREACH_MODE={mode}\n"
        env_path.write_text(text)
        os.environ["INREACH_MODE"] = mode
        logger.info("Mode bascule : %s", mode)
        return jsonify({"ok": True, "mode": mode})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
