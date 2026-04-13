"""
blueprints/weather.py — Routes météo : weather snapshots, forecasts, grib index.
"""
import json
import logging

from flask import Blueprint, jsonify
from flask_login import login_required

from blueprints.shared import get_db, BASE_DIR

logger = logging.getLogger("sailtracker_server")

bp = Blueprint("weather", __name__)

GRIB_CACHE_DIR = BASE_DIR / "static" / "grib_cache"


@bp.route("/api/weather/latest")
@login_required
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


@bp.route("/api/weather/forecast")
@login_required
def api_weather_forecast():
    conn = get_db()
    wind_rows = conn.execute(
        "SELECT forecast_time, value1, value2, value3 FROM weather_forecasts "
        "WHERE data_type='wind' AND forecast_time>=datetime('now') "
        "AND collected_at=(SELECT MAX(collected_at) FROM weather_forecasts WHERE data_type='wind') "
        "ORDER BY forecast_time ASC LIMIT 72"
    ).fetchall()
    wave_rows = conn.execute(
        "SELECT forecast_time, value1, value2, value3 FROM weather_forecasts "
        "WHERE data_type='wave' AND forecast_time>=datetime('now') "
        "AND collected_at=(SELECT MAX(collected_at) FROM weather_forecasts WHERE data_type='wave') "
        "ORDER BY forecast_time ASC LIMIT 72"
    ).fetchall()
    conn.close()
    return jsonify({
        "wind_forecast": [{"time": r["forecast_time"], "speed_kmh": r["value1"], "direction_deg": r["value2"], "gusts_kmh": r["value3"]} for r in wind_rows],
        "wave_forecast": [{"time": r["forecast_time"], "height_m": r["value1"], "direction_deg": r["value2"], "period_s": r["value3"]} for r in wave_rows],
    })


@bp.route("/api/grib/index")
@login_required
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
