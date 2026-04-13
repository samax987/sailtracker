"""
blueprints/sailing.py — Configurations de voiles, observations et dashboard de quart.
"""
import json
import logging
import math
from datetime import datetime, timezone

import requests
from flask import Blueprint, jsonify, request, render_template
from flask_login import login_required, current_user

from .shared import get_db, haversine_nm, minutes_ago
from polars import get_polar

logger = logging.getLogger("sailtracker_server")

bp = Blueprint("sailing", __name__)


# =============================================================================
# API : Configurations de voiles (sail_config_periods)
# =============================================================================

@bp.route("/api/sail-configs", methods=["GET"])
@login_required
def api_sail_configs_list():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM sail_config_periods ORDER BY timestamp_start DESC"
        ).fetchall()
        return jsonify({"configs": [dict(r) for r in rows]})
    finally:
        conn.close()


@bp.route("/api/sail-configs", methods=["POST"])
@login_required
def api_sail_configs_add():
    data = request.get_json() or {}
    conn = get_db()
    try:
        ts_start = data.get("timestamp_start", "")
        if not ts_start:
            return jsonify({"error": "timestamp_start requis"}), 400
        reef = max(0, min(4, int(data.get("reef_count", 0))))
        genoa = max(0, min(100, int(data.get("genoa_pct", 100))))
        spinnaker = 1 if data.get("spinnaker") else 0
        ts_end = data.get("timestamp_end") or None
        desc = data.get("description", "")[:200]
        if not desc:
            parts = []
            if reef > 0: parts.append(f"{reef} ris")
            if genoa < 100: parts.append(f"génois {genoa}%")
            if spinnaker: parts.append("spi")
            desc = ("Plein voile" if not parts else " + ".join(parts))
        cur = conn.execute(
            """INSERT INTO sail_config_periods
               (timestamp_start, timestamp_end, reef_count, genoa_pct, spinnaker, description)
               VALUES (?,?,?,?,?,?)""",
            (ts_start, ts_end, reef, genoa, spinnaker, desc)
        )
        config_id = cur.lastrowid
        q = "UPDATE polar_observations SET sail_config_id=? WHERE sail_config_id IS NULL AND timestamp >= ?"
        params = [config_id, ts_start]
        if ts_end:
            q += " AND timestamp <= ?"
            params.append(ts_end)
        tagged = conn.execute(q, params).rowcount
        conn.commit()
        return jsonify({"id": config_id, "description": desc, "tagged_obs": tagged}), 201
    finally:
        conn.close()


@bp.route("/api/sail-configs/<int:config_id>", methods=["DELETE"])
def api_sail_configs_delete(config_id):
    conn = get_db()
    try:
        if not conn.execute("SELECT id FROM sail_config_periods WHERE id=?", (config_id,)).fetchone():
            return jsonify({"error": "Config non trouvée"}), 404
        conn.execute("UPDATE polar_observations SET sail_config_id=NULL WHERE sail_config_id=?", (config_id,))
        conn.execute("DELETE FROM sail_config_periods WHERE id=?", (config_id,))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()


@bp.route("/api/sail-configs/stats")
def api_sail_configs_stats():
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM polar_observations WHERE is_valid=1").fetchone()[0]
        full_sail = conn.execute(
            """SELECT COUNT(*) FROM polar_observations po
               LEFT JOIN sail_config_periods sc ON po.sail_config_id = sc.id
               WHERE po.is_valid=1
                 AND (po.sail_config_id IS NULL
                      OR (sc.reef_count=0 AND sc.genoa_pct >= 90 AND sc.spinnaker=0))"""
        ).fetchone()[0]
        reduced = total - full_sail
        by_config = conn.execute(
            """SELECT sc.description, sc.reef_count, sc.genoa_pct, sc.spinnaker,
                      COUNT(po.id) as n_obs
               FROM sail_config_periods sc
               LEFT JOIN polar_observations po ON po.sail_config_id = sc.id AND po.is_valid=1
               GROUP BY sc.id ORDER BY sc.timestamp_start DESC"""
        ).fetchall()
        return jsonify({
            "total_obs": total,
            "full_sail_obs": full_sail,
            "reduced_sail_obs": reduced,
            "by_config": [dict(r) for r in by_config],
        })
    finally:
        conn.close()


@bp.route("/api/sail-configs/active")
def api_sail_configs_active():
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT id, timestamp_start, timestamp_end,
                      reef_count, genoa_pct, spinnaker, description
               FROM sail_config_periods
               WHERE timestamp_end IS NULL
                  OR timestamp_end >= datetime('now')
               ORDER BY timestamp_start DESC LIMIT 1"""
        ).fetchone()
        if row:
            return jsonify({"active": dict(row)})
        return jsonify({"active": None})
    finally:
        conn.close()


@bp.route("/api/sail-configs/quick-change", methods=["POST"])
def api_sail_configs_quick_change():
    data = request.get_json() or {}
    reef     = max(0, min(4, int(data.get("reef_count", 0))))
    genoa    = max(0, min(100, int(data.get("genoa_pct", 100))))
    spinnaker = 1 if data.get("spinnaker") else 0
    route_id  = data.get("route_id")

    parts = []
    if spinnaker:
        label = "Spinnaker"
    else:
        if reef > 0: parts.append(f"{reef} ris")
        if genoa < 100: parts.append(f"génois {genoa}%")
        label = "Plein voile" if not parts else " + ".join(parts)

    conn = get_db()
    try:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        prev = conn.execute(
            """SELECT description FROM sail_config_periods
               WHERE timestamp_end IS NULL ORDER BY timestamp_start DESC LIMIT 1"""
        ).fetchone()
        prev_label = prev["description"] if prev else "inconnue"
        conn.execute(
            "UPDATE sail_config_periods SET timestamp_end=? WHERE timestamp_end IS NULL",
            (now_str,)
        )
        cur = conn.execute(
            """INSERT INTO sail_config_periods
               (timestamp_start, reef_count, genoa_pct, spinnaker, description)
               VALUES (?,?,?,?,?)""",
            (now_str, reef, genoa, spinnaker, label)
        )
        config_id = cur.lastrowid
        log_content = f"{prev_label} → {label}"
        if route_id:
            try:
                conn.execute(
                    """INSERT INTO logbook_entries
                       (route_id, entry_time, entry_type, content)
                       VALUES (?,?,?,?)""",
                    (route_id, now_str, "sail_change", log_content)
                )
            except Exception:
                pass
        conn.commit()
        return jsonify({"id": config_id, "description": label, "log": log_content}), 201
    finally:
        conn.close()


@bp.route("/api/sail-observation", methods=["POST"])
@login_required
def api_sail_observation():
    data = request.get_json() or {}
    if "actual_reef" not in data or "actual_genoa" not in data:
        return jsonify({"error": "Champs manquants"}), 400
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO sail_config_observations
               (tws, twa, rec_reef, rec_genoa, rec_spi,
                actual_reef, actual_genoa, actual_spi, sog)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                data.get("tws"), data.get("twa"),
                data.get("rec_reef"), data.get("rec_genoa", 100), data.get("rec_spi", 0),
                data["actual_reef"], data["actual_genoa"], data.get("actual_spi", 0),
                data.get("sog"),
            )
        )
        conn.commit()
        return jsonify({"ok": True}), 201
    except Exception as e:
        logger.error("sail_observation INSERT: %s", e)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@bp.route("/api/sail-preferences")
@login_required
def api_sail_preferences():
    DEFAULT_BANDS = [
        {"tws_min": 0,  "tws_max": 8,  "reef": 0, "genoa": 100},
        {"tws_min": 8,  "tws_max": 15, "reef": 0, "genoa": 100},
        {"tws_min": 15, "tws_max": 20, "reef": 1, "genoa": 100},
        {"tws_min": 20, "tws_max": 25, "reef": 2, "genoa": 80},
        {"tws_min": 25, "tws_max": 30, "reef": 3, "genoa": 50},
        {"tws_min": 30, "tws_max": 38, "reef": 4, "genoa": 30},
        {"tws_min": 38, "tws_max": 99, "reef": 4, "genoa": 0},
    ]
    conn = get_db()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM sail_config_observations"
        ).fetchone()[0]
        bands = []
        for band in DEFAULT_BANDS:
            rows = conn.execute(
                """SELECT actual_reef, actual_genoa, COUNT(*) as n
                   FROM sail_config_observations
                   WHERE actual_spi = 0 AND tws >= ? AND tws < ?
                   GROUP BY actual_reef, actual_genoa
                   ORDER BY n DESC""",
                (band["tws_min"], band["tws_max"])
            ).fetchall()
            n_total = sum(r["n"] for r in rows)
            if n_total >= 3 and rows:
                obs_reef  = sum(r["actual_reef"]  * r["n"] for r in rows) / n_total
                obs_genoa = sum(r["actual_genoa"] * r["n"] for r in rows) / n_total
                w = min(0.7, n_total / 10 * 0.7)
                adapted_reef  = round(band["reef"]  * (1 - w) + obs_reef  * w)
                adapted_genoa = round(band["genoa"] * (1 - w) + obs_genoa * w)
            else:
                adapted_reef  = band["reef"]
                adapted_genoa = band["genoa"]
                n_total = 0
            bands.append({
                **band,
                "adapted_reef":  adapted_reef,
                "adapted_genoa": adapted_genoa,
                "n_obs": n_total,
            })
        return jsonify({"bands": bands, "total_observations": total})
    except Exception as e:
        logger.error("sail_preferences: %s", e)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# =============================================================================
# Dashboard de quart
# =============================================================================

# Cache vent pour éviter de spammer Open-Meteo (TTL 15 min)
_quart_wind_cache: dict = {}


def _fetch_quart_wind(lat: float, lon: float) -> dict:
    """Fetch vent actuel + prévision 12h à la position du bateau (Open-Meteo)."""
    import time as _time
    now_ts = _time.time()
    cached = _quart_wind_cache.get("data")
    cached_at = _quart_wind_cache.get("ts", 0)
    cached_lat = _quart_wind_cache.get("lat")
    cached_lon = _quart_wind_cache.get("lon")
    if (cached and now_ts - cached_at < 900
            and cached_lat is not None
            and haversine_nm(lat, lon, cached_lat, cached_lon) < 5):
        return cached

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": round(lat, 3), "longitude": round(lon, 3),
        "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
        "wind_speed_unit": "kn",
        "models": "ecmwf_ifs025",
        "past_hours": 1,
        "forecast_hours": 13,
    }
    try:
        resp = requests.get(url, params=params, timeout=15,
                            headers={"User-Agent": "SailTracker/1.0"})
        resp.raise_for_status()
        data = resp.json()
        hourly = data.get("hourly", {})
        times  = hourly.get("time", [])
        speeds = hourly.get("wind_speed_10m", [])
        dirs   = hourly.get("wind_direction_10m", [])
        gusts  = hourly.get("wind_gusts_10m", [])

        from datetime import datetime as _dt, timezone as _tz
        now_naive = _dt.now(_tz.utc).replace(tzinfo=None)
        best_idx, best_diff = 0, float("inf")
        for i, t in enumerate(times):
            try:
                td = abs((_dt.fromisoformat(t) - now_naive).total_seconds())
                if td < best_diff:
                    best_diff, best_idx = td, i
            except Exception:
                continue

        def _v(arr, i): return round(arr[i], 1) if i < len(arr) and arr[i] is not None else None

        current = {
            "tws_kts": _v(speeds, best_idx),
            "twd_deg": _v(dirs, best_idx),
            "gusts_kts": _v(gusts, best_idx),
        }
        forecast_12h = []
        for offset in range(1, 13):
            idx = best_idx + offset
            if idx < len(times):
                forecast_12h.append({
                    "label": f"+{offset}h",
                    "tws": _v(speeds, idx),
                    "twd": _v(dirs, idx),
                    "gusts": _v(gusts, idx),
                })

        result = {"current": current, "forecast_12h": forecast_12h}
        _quart_wind_cache.update({"data": result, "ts": now_ts, "lat": lat, "lon": lon})
        return result
    except Exception as e:
        logger.warning("quart wind fetch error: %s", e)
        return {"current": {}, "forecast_12h": []}


def _twa_label(twa: float) -> str:
    if twa < 35:  return "Près serré"
    if twa < 60:  return "Près"
    if twa < 90:  return "Petit largue"
    if twa < 120: return "Largue"
    if twa < 150: return "Grand largue"
    return "Vent arrière"


def _wind_dir_arrow(twd: float) -> str:
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSO","SO","OSO","O","ONO","NO","NNO"]
    return dirs[int((twd + 11.25) / 22.5) % 16]


@bp.route("/quart")
@login_required
def quart_page():
    return render_template("quart.html")


@bp.route("/api/quart")
@login_required
def api_quart():
    conn = get_db()
    try:
        pos_row = conn.execute(
            """SELECT latitude, longitude, speed_knots, course, timestamp
               FROM positions WHERE source='inreach'
               ORDER BY timestamp DESC LIMIT 1"""
        ).fetchone()
        if not pos_row:
            return jsonify({"error": "Aucune position InReach"}), 404

        boat_lat  = float(pos_row["latitude"])
        boat_lon  = float(pos_row["longitude"])
        boat_sog  = round(float(pos_row["speed_knots"] or 0), 1)
        boat_cog  = round(float(pos_row["course"] or 0), 0)
        pos_ts    = pos_row["timestamp"]
        pos_age   = minutes_ago(pos_ts)

        wind_data = _fetch_quart_wind(boat_lat, boat_lon)
        current_wind = wind_data.get("current", {})
        tws = current_wind.get("tws_kts")
        twd = current_wind.get("twd_deg")
        gusts = current_wind.get("gusts_kts")

        twa = None
        twa_label = None
        if twd is not None:
            raw = (twd - boat_cog + 360) % 360
            twa = round(raw if raw <= 180 else 360 - raw, 0)
            twa_label = _twa_label(twa)

        wind_arrow = _wind_dir_arrow(twd) if twd is not None else None

        polar_target = None
        polar_efficiency = None
        if twa is not None and tws is not None:
            try:
                polar = get_polar(user_id=current_user.id)
                target = polar.get_boat_speed(twa, tws)
                if target > 0.1:
                    polar_target = round(target, 1)
                    polar_efficiency = min(100, round(boat_sog / target * 100))
            except Exception:
                pass

        route_info = None
        active_route = conn.execute(
            "SELECT id, name, waypoints FROM passage_routes WHERE phase='active' AND user_id=? LIMIT 1",
            (current_user.id,)
        ).fetchone()
        if active_route:
            wps = json.loads(active_route["waypoints"] or "[]")
            if wps:
                next_wp = None
                min_dist = float("inf")
                for wp in wps:
                    d = haversine_nm(boat_lat, boat_lon, wp["lat"], wp["lon"])
                    if d < min_dist:
                        min_dist = d
                        next_wp = wp

                try:
                    wp_idx = next((i for i, w in enumerate(wps)
                                   if abs(w["lat"] - next_wp["lat"]) < 0.01), 0)
                    remaining_wps = wps[wp_idx:]
                    nm_remaining = haversine_nm(boat_lat, boat_lon,
                                               remaining_wps[0]["lat"], remaining_wps[0]["lon"])
                    for i in range(1, len(remaining_wps)):
                        nm_remaining += haversine_nm(remaining_wps[i-1]["lat"], remaining_wps[i-1]["lon"],
                                                     remaining_wps[i]["lat"], remaining_wps[i]["lon"])
                except Exception:
                    nm_remaining = min_dist

                dlon = math.radians(next_wp["lon"] - boat_lon)
                lat1r, lat2r = math.radians(boat_lat), math.radians(next_wp["lat"])
                x = math.sin(dlon) * math.cos(lat2r)
                y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
                bearing_to_wp = round((math.degrees(math.atan2(x, y)) + 360) % 360, 0)

                vmg = None
                if boat_sog > 0 and len(wps) > 0:
                    final_wp = wps[-1]
                    bear_final = (math.degrees(math.atan2(
                        math.sin(math.radians(final_wp["lon"] - boat_lon)) * math.cos(math.radians(final_wp["lat"])),
                        math.cos(math.radians(boat_lat)) * math.sin(math.radians(final_wp["lat"])) -
                        math.sin(math.radians(boat_lat)) * math.cos(math.radians(final_wp["lat"])) *
                        math.cos(math.radians(final_wp["lon"] - boat_lon))
                    )) + 360) % 360
                    vmg = round(boat_sog * math.cos(math.radians(boat_cog - bear_final)), 1)

                eta_str = None
                if vmg and vmg > 0.5:
                    from datetime import timedelta
                    hours_left = nm_remaining / vmg
                    eta_dt = datetime.now(timezone.utc) + timedelta(hours=hours_left)
                    if hours_left < 48:
                        eta_str = eta_dt.strftime("%d/%m %H:%Mz")
                    else:
                        days = int(hours_left / 24)
                        eta_str = f"J+{days} ({eta_dt.strftime('%d/%m')})"

                route_info = {
                    "route_id": active_route["id"],
                    "route_name": active_route["name"],
                    "next_wp_name": next_wp.get("name", f"WP{wp_idx+1}"),
                    "next_wp_dist_nm": round(min_dist, 1),
                    "next_wp_bearing": bearing_to_wp,
                    "nm_remaining": round(nm_remaining, 0),
                    "vmg": vmg,
                    "eta": eta_str,
                }

        sail_config = None
        try:
            sc_row = conn.execute(
                """SELECT id, reef_count, genoa_pct, spinnaker, description
                   FROM sail_config_periods
                   WHERE timestamp_end IS NULL
                      OR timestamp_end >= datetime('now')
                   ORDER BY timestamp_start DESC LIMIT 1"""
            ).fetchone()
            if sc_row:
                sail_config = dict(sc_row)
        except Exception:
            pass

        recent_logs = []
        try:
            log_rows = conn.execute(
                """SELECT timestamp, entry_type, text FROM logbook_entries
                   WHERE user_id=?
                   ORDER BY timestamp DESC LIMIT 5""",
                (current_user.id,)
            ).fetchall()
            for r in log_rows:
                recent_logs.append({
                    "time": (r["timestamp"] or "")[:16].replace("T", " "),
                    "type": r["entry_type"],
                    "content": (r["text"] or "")[:80],
                })
        except Exception:
            pass

        return jsonify({
            "position": {
                "lat": round(boat_lat, 4),
                "lon": round(boat_lon, 4),
                "sog": boat_sog,
                "cog": int(boat_cog),
                "timestamp": pos_ts,
                "age_min": pos_age,
            },
            "wind": {
                "tws_kts": tws,
                "twd_deg": twd,
                "twa_deg": int(twa) if twa is not None else None,
                "twa_label": twa_label,
                "wind_arrow": wind_arrow,
                "gusts_kts": gusts,
            },
            "polar": {
                "target_kts": polar_target,
                "efficiency_pct": polar_efficiency,
            },
            "route": route_info,
            "forecast_12h": wind_data.get("forecast_12h", []),
            "sail_config": sail_config,
            "logbook": recent_logs,
        })
    finally:
        conn.close()
