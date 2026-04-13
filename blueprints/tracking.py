from flask import Blueprint, jsonify, request
from flask_login import login_required
from .shared import get_db, minutes_ago

bp = Blueprint("tracking", __name__)


# =============================================================================
# API : positions
# =============================================================================

@bp.route("/api/position/latest")
@login_required
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


@bp.route("/api/position/track")
@login_required
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

@bp.route("/api/status")
@login_required
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
