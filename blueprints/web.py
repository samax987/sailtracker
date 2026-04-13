"""
blueprints/web.py — Pages web principales (HTML) de SailTracker.
"""
import math
from flask import Blueprint, render_template, send_from_directory, request
from flask_login import login_required

from .shared import (
    get_db, haversine_nm, STATIC_DIR,
    VERIF_ZONES_ORDERED, VERIF_ZONE_LABELS, VERIF_ZONES,
)

bp = Blueprint("web", __name__)


# =============================================================================
# Pages statiques / index
# =============================================================================

@bp.route("/")
def index():
    """Page principale — redirige vers mobile si User-Agent mobile."""
    import re
    MOBILE_UA = re.compile(
        r'Mobile|Android|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini',
        re.IGNORECASE
    )
    ua = request.headers.get('User-Agent', '')
    if MOBILE_UA.search(ua):
        return send_from_directory(str(STATIC_DIR), 'index_mobile.html')
    return send_from_directory(str(STATIC_DIR), 'index.html')


@bp.route("/mobile")
def mobile_index():
    return send_from_directory(str(STATIC_DIR), 'index_mobile.html')


@bp.route("/passage")
def passage_page():
    return send_from_directory(str(STATIC_DIR), "passage.html")


@bp.route("/polars")
def polars_page():
    return render_template("polars.html")


@bp.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(str(STATIC_DIR), filename)


# =============================================================================
# Page accuracy — comparaison des modèles météo
# =============================================================================

@bp.route("/accuracy")
def accuracy_page():
    conn = get_db()
    try:
        count_row = conn.execute(
            "SELECT COUNT(DISTINCT date) as n FROM model_accuracy"
        ).fetchone()
        days_count = count_row["n"] if count_row else 0

        acc_rows = conn.execute(
            """SELECT model, zone, forecast_hour,
                      AVG(wind_speed_error_avg) as avg_err,
                      AVG(wind_dir_error_avg) as avg_dir_err,
                      AVG(zone_lat) as avg_lat,
                      AVG(zone_lon) as avg_lon,
                      COUNT(*) as n_days
               FROM model_accuracy
               WHERE date >= date('now', '-30 days')
                 AND zone IN ('local','near','regional','ocean')
               GROUP BY model, zone, forecast_hour
               ORDER BY zone, forecast_hour, avg_err ASC"""
        ).fetchall()

        zones_in_data = sorted(set(r["zone"] for r in acc_rows),
                               key=lambda z: VERIF_ZONES_ORDERED.index(z)
                               if z in VERIF_ZONES_ORDERED else 99)
        zones = zones_in_data if zones_in_data else VERIF_ZONES_ORDERED
        horizons = [1, 2, 3, 5, 7]
        models = ['ecmwf_ifs025', 'gfs_seamless', 'icon_seamless']

        zone_positions = {}
        for r in acc_rows:
            z = r["zone"]
            if z not in zone_positions and r["avg_lat"] is not None:
                zone_positions[z] = {
                    "lat": round(r["avg_lat"], 2),
                    "lon": round(r["avg_lon"], 2),
                }

        boat_row = conn.execute(
            """SELECT latitude, longitude FROM positions
               WHERE source='inreach' ORDER BY timestamp DESC LIMIT 1"""
        ).fetchone()
        boat_lat = float(boat_row["latitude"]) if boat_row else None
        boat_lon = float(boat_row["longitude"]) if boat_row else None

        zone_distances = {}
        if boat_lat is not None:
            for z, pos in zone_positions.items():
                d = haversine_nm(boat_lat, boat_lon, pos["lat"], pos["lon"])
                zone_distances[z] = round(d)

        zone_display = {}
        for z in zones:
            base = VERIF_ZONE_LABELS.get(z, z)
            dist = zone_distances.get(z)
            if dist is not None and z != 'local':
                zone_display[z] = f"{base} ({dist} nm)"
            else:
                zone_display[z] = base

        model_errors = {}
        for r in acc_rows:
            z, m = r["zone"], r["model"]
            h = round(r["forecast_hour"] / 24)
            if z not in model_errors: model_errors[z] = {}
            if m not in model_errors[z]: model_errors[z][m] = {}
            if r["avg_err"] is not None:
                model_errors[z][m][h] = {
                    "speed": round(r["avg_err"], 2),
                    "dir": round(r["avg_dir_err"], 1) if r["avg_dir_err"] is not None else None,
                }

        global_scores = {}
        for m in models:
            errs = [
                model_errors.get(z, {}).get(m, {}).get(h, {}).get("speed")
                for z in zones for h in horizons
            ]
            errs = [e for e in errs if e is not None]
            global_scores[m] = round(sum(errs) / len(errs), 2) if errs else None

        zone_best = {}
        for z in zones:
            best_m, best_e = None, float('inf')
            for m in models:
                errs = [model_errors.get(z, {}).get(m, {}).get(h, {}).get("speed") for h in horizons]
                errs = [e for e in errs if e is not None]
                if errs:
                    avg = sum(errs) / len(errs)
                    if avg < best_e:
                        best_e, best_m = avg, m
            zone_best[z] = {"model": best_m, "error": round(best_e, 2)} if best_m else None

        best_table = {}
        for z in zones:
            best_table[z] = {}
            for h in horizons:
                best_table[z][h] = {}
                for m in models:
                    e = model_errors.get(z, {}).get(m, {}).get(h, {})
                    best_table[z][h][m] = e if e else None

        chart_data, chart_dir_data = {}, {}
        for z in zones:
            chart_data[z], chart_dir_data[z] = {}, {}
            for m in models:
                chart_data[z][m] = [
                    model_errors.get(z, {}).get(m, {}).get(h, {}).get("speed") for h in horizons
                ]
                chart_dir_data[z][m] = [
                    model_errors.get(z, {}).get(m, {}).get(h, {}).get("dir") for h in horizons
                ]

        trend_rows = conn.execute(
            """SELECT model, date, AVG(wind_speed_error_avg) as avg_err
               FROM model_accuracy
               WHERE date >= date('now', '-30 days')
               GROUP BY model, date
               ORDER BY date"""
        ).fetchall()
        trend_dates = sorted(set(r["date"] for r in trend_rows))
        trend_by = {}
        for r in trend_rows:
            trend_by.setdefault(r["model"], {})[r["date"]] = (
                round(r["avg_err"], 2) if r["avg_err"] is not None else None
            )
        trend_data = {
            m: [trend_by.get(m, {}).get(d) for d in trend_dates]
            for m in models
        }

        sorted_models = sorted(
            [(m, global_scores[m]) for m in models if global_scores.get(m) is not None],
            key=lambda x: x[1]
        )
        global_scores_ranked = [
            {"rank": i+1, "model": m, "score": s} for i, (m, s) in enumerate(sorted_models)
        ]
        worst_score = max(s for _, s in sorted_models) if sorted_models else 10

        zone_mini_bars = {}
        for z in zones:
            bars = []
            for m in models:
                e = model_errors.get(z, {}).get(m, {}).get(1, {}).get("speed")
                if e is not None:
                    bars.append({"model": m, "error": e})
            bars.sort(key=lambda x: x["error"])
            max_e = bars[-1]["error"] if bars else 1
            for b in bars:
                b["bar_w"] = min(100, max(10, int((max_e - b["error"]) / max_e * 80 + 20)))
            zone_mini_bars[z] = bars

        comp_rows = []
        for z in zones:
            for h in horizons:
                cells = {}
                speed_vals = {}
                for m in models:
                    e = model_errors.get(z, {}).get(m, {}).get(h, {})
                    s = e.get("speed") if e else None
                    d = e.get("dir") if e else None
                    cells[m] = {"speed": s, "dir": d}
                    if s is not None:
                        speed_vals[m] = s
                min_speed = min(speed_vals.values()) if speed_vals else None
                max_speed = max(speed_vals.values()) if speed_vals else 10
                for m in cells:
                    s = cells[m]["speed"]
                    cells[m]["is_best"] = (s is not None and s == min_speed)
                    cells[m]["bar_w"] = (
                        min(100, max(10, int((max_speed - s) / max_speed * 80 + 20)))
                        if s is not None and max_speed > 0 else 10
                    )
                comp_rows.append({
                    "zone": z, "horizon": h, "cells": cells,
                    "first_for_zone": (h == horizons[0]),
                    "zone_rowspan": len(horizons),
                })

        return render_template('accuracy.html',
            days_count=days_count,
            zones=zones,
            horizons=horizons,
            models=models,
            model_errors=model_errors,
            global_scores=global_scores,
            global_scores_ranked=global_scores_ranked,
            worst_score=worst_score,
            zone_best=zone_best,
            zone_mini_bars=zone_mini_bars,
            zone_display=zone_display,
            zone_positions=zone_positions,
            zone_distances=zone_distances,
            boat_lat=boat_lat,
            boat_lon=boat_lon,
            comp_rows=comp_rows,
            chart_data=chart_data,
            chart_dir_data=chart_dir_data,
            trend_data=trend_data,
            trend_dates=trend_dates,
        )
    finally:
        conn.close()


@bp.route('/analysis')
@login_required
def analysis_page():
    return render_template('analysis.html')
