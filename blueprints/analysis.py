"""
blueprints/analysis.py — Routes API polaires (polars) de SailTracker.
"""
import math
import re
import shutil
import subprocess
from pathlib import Path

from flask import Blueprint, jsonify, request, make_response
from flask_login import login_required

from .shared import get_db, BASE_DIR

bp = Blueprint("analysis", __name__)

# Import du module polaires (au niveau du package)
from polars import get_polar, reload_polar  # noqa: E402


# =============================================================================
# API : Polaires
# =============================================================================

@bp.route("/api/polars", methods=["GET"])
def api_polars_get():
    try:
        return jsonify(get_polar().to_dict())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/polars", methods=["PUT"])
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


@bp.route("/api/polars/reset", methods=["POST"])
def api_polars_reset():
    try:
        src = BASE_DIR / "data" / "polars" / "pollen1_default.csv"
        dst = BASE_DIR / "data" / "polars" / "pollen1.csv"
        shutil.copy2(str(src), str(dst))
        reload_polar()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/polars/export")
def api_polars_export():
    polar_path = BASE_DIR / "data" / "polars" / "pollen1.csv"
    response = make_response(polar_path.read_text(encoding="utf-8"))
    response.headers["Content-Type"] = "text/csv"
    response.headers["Content-Disposition"] = "attachment; filename=pollen1.csv"
    return response


@bp.route("/api/polars/speed")
def api_polars_speed():
    try:
        twa = float(request.args.get("twa", 0))
        tws = float(request.args.get("tws", 0))
        speed = get_polar().get_boat_speed(twa, tws)
        return jsonify({"twa": twa, "tws": tws, "boat_speed_kts": round(speed, 2)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/polars/observations")
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


@bp.route("/api/polars/comparison")
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


@bp.route("/api/polars/calibrate", methods=["POST"])
def api_polars_calibrate():
    """Lance la calibration des polaires depuis les observations InReach."""
    try:
        result = subprocess.run(
            ["venv/bin/python3", "polar_calibrator.py"],
            cwd=str(BASE_DIR),
            capture_output=True, text=True, timeout=120
        )
        new_obs = 0
        updated_cells = 0
        for line in (result.stdout + result.stderr).splitlines():
            m = re.search(r"(\d+) nouvelles observations", line)
            if m: new_obs = int(m.group(1))
            m = re.search(r"(\d+) cases mises à jour", line)
            if m: updated_cells = int(m.group(1))
        try:
            log_path = BASE_DIR / "logs" / "polar_calibration.log"
            lines = log_path.read_text().splitlines()[-10:]
            for line in lines:
                m = re.search(r"(\d+) nouvelles observations", line)
                if m: new_obs = int(m.group(1))
                m = re.search(r"(\d+) cases mises à jour", line)
                if m: updated_cells = int(m.group(1))
        except Exception:
            pass
        if result.returncode != 0 and not new_obs:
            return jsonify({"success": False, "error": result.stderr[-300:] or "Erreur inconnue"})
        return jsonify({"success": True, "new_obs": new_obs, "updated_cells": updated_cells})
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": "Timeout (>120s)"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
