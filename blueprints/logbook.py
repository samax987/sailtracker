import json
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request, render_template
from flask_login import login_required, current_user
from .shared import get_db, haversine_nm

bp = Blueprint("logbook", __name__)


# =============================================================================
# API : Journal de bord (Logbook)
# =============================================================================

@bp.route("/api/logbook/<int:route_id>", methods=["GET"])
def api_logbook_list(route_id):
    conn = get_db()
    try:
        entry_type = request.args.get("type")
        limit = min(int(request.args.get("limit", 100)), 500)
        query = "SELECT * FROM logbook_entries WHERE route_id=?"
        params = [route_id]
        if entry_type:
            query += " AND entry_type=?"
            params.append(entry_type)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return jsonify({"entries": [dict(r) for r in rows]})
    finally:
        conn.close()


@bp.route("/api/logbook/<int:route_id>", methods=["POST"])
def api_logbook_add(route_id):
    data = request.get_json() or {}
    conn = get_db()
    try:
        if not conn.execute("SELECT id FROM passage_routes WHERE id=?", (route_id,)).fetchone():
            return jsonify({"error": "Route non trouvée"}), 404
        text = data.get("text", "")[:2000]
        entry_type = data.get("entry_type", "note")
        if entry_type not in ("note", "weather", "sail_change", "incident", "waypoint", "auto"):
            entry_type = "note"
        # Position : depuis données ou dernière position InReach
        lat = data.get("latitude")
        lon = data.get("longitude")
        if lat is None:
            pos = conn.execute(
                "SELECT latitude, longitude FROM positions WHERE user_id=? ORDER BY timestamp DESC LIMIT 1",
                (current_user.id,)
            ).fetchone()
            if pos:
                lat, lon = pos["latitude"], pos["longitude"]
        timestamp = data.get("timestamp") or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            """INSERT INTO logbook_entries
               (route_id, user_id, timestamp, entry_type, text, latitude, longitude,
                wind_speed_kts, wind_dir_deg, sog_kts, sea_state, sail_config, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (route_id, current_user.id, timestamp, entry_type, text, lat, lon,
             data.get("wind_speed_kts"), data.get("wind_dir_deg"),
             data.get("sog_kts"), data.get("sea_state"), data.get("sail_config"),
             data.get("created_by", "manual"))
        )
        conn.commit()
        return jsonify({"id": cur.lastrowid, "success": True}), 201
    finally:
        conn.close()


@bp.route("/api/logbook/entry/<int:entry_id>", methods=["DELETE"])
def api_logbook_delete(entry_id):
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM logbook_entries WHERE id=?", (entry_id,)).fetchone()
        if not row:
            return jsonify({"error": "Entrée non trouvée"}), 404
        conn.execute("DELETE FROM logbook_entries WHERE id=?", (entry_id,))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()


@bp.route("/api/logbook/entry/<int:entry_id>", methods=["PUT"])
def api_logbook_update(entry_id):
    data = request.get_json() or {}
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM logbook_entries WHERE id=?", (entry_id,)).fetchone()
        if not row:
            return jsonify({"error": "Entrée non trouvée"}), 404
        text = data.get("text", "")[:2000]
        conn.execute("UPDATE logbook_entries SET text=? WHERE id=?", (text, entry_id))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()


@bp.route("/logbook/<int:route_id>")
def logbook_page(route_id):
    conn = get_db()
    try:
        route = conn.execute(
            "SELECT id, name, phase, actual_departure, actual_arrival FROM passage_routes WHERE id=?",
            (route_id,)
        ).fetchone()
        if not route:
            return "Route non trouvée", 404
        return render_template("logbook.html", route=dict(route))
    finally:
        conn.close()


# =============================================================================
# API : Replay de traversée
# =============================================================================

@bp.route("/api/replay/<int:route_id>")
def api_replay(route_id):
    conn = get_db()
    try:
        route = conn.execute("SELECT * FROM passage_routes WHERE id=?", (route_id,)).fetchone()
        if not route:
            return jsonify({"error": "Route non trouvée"}), 404

        waypoints = json.loads(route["waypoints"])
        dep = route["actual_departure"]
        arr = route["actual_arrival"]

        # Positions InReach pendant la traversée
        query = "SELECT timestamp, latitude, longitude, speed_knots, course FROM positions WHERE source='inreach'"
        params = []
        if dep:
            query += " AND timestamp >= ?"
            params.append(dep)
        if arr:
            query += " AND timestamp <= ?"
            params.append(arr)
        query += " ORDER BY timestamp ASC"
        positions = conn.execute(query, params).fetchall()

        # Stats globales
        total_nm = 0.0
        pos_list = list(positions)
        for i in range(1, len(pos_list)):
            total_nm += haversine_nm(
                pos_list[i-1]["latitude"], pos_list[i-1]["longitude"],
                pos_list[i]["latitude"], pos_list[i]["longitude"]
            )
        speeds = [p["speed_knots"] for p in pos_list if p["speed_knots"] and p["speed_knots"] > 0]
        avg_sog = round(sum(speeds) / len(speeds), 2) if speeds else None
        max_sog = round(max(speeds), 2) if speeds else None

        # Entrées logbook pour la carte
        logbook = {r["timestamp"][:16]: r["text"] for r in conn.execute(
            "SELECT timestamp, text FROM logbook_entries WHERE route_id=? ORDER BY timestamp ASC",
            (route_id,)
        ).fetchall()}

        track = []
        for p in pos_list:
            ts = p["timestamp"][:16]
            track.append({
                "ts": p["timestamp"],
                "lat": p["latitude"],
                "lon": p["longitude"],
                "sog": p["speed_knots"],
                "cog": p["course"],
                "logbook": logbook.get(ts),
            })

        return jsonify({
            "route": {"id": route_id, "name": route["name"], "waypoints": waypoints},
            "actual_departure": dep,
            "actual_arrival": arr,
            "track": track,
            "stats": {
                "total_nm": round(total_nm, 0),
                "n_positions": len(track),
                "avg_sog": avg_sog,
                "max_sog": max_sog,
            },
        })
    finally:
        conn.close()


@bp.route("/replay/<int:route_id>")
def replay_page(route_id):
    return render_template("replay.html", route_id=route_id)
