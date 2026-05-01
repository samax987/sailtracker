"""
shared.py — Utilitaires partagés entre tous les blueprints.
"""
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import os

from dotenv import load_dotenv
from flask_login import UserMixin

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = BASE_DIR / "sailtracker.db"
STATIC_DIR = BASE_DIR / "static"
GRIB_CACHE_DIR = STATIC_DIR / "grib_cache"

# Zones de vérification météo
VERIF_ZONES_ORDERED = ['local', 'near', 'regional', 'ocean']
VERIF_ZONE_LABELS = {
    'local':    'Local',
    'near':     '~150 nm',
    'regional': '~400 nm',
    'ocean':    '~800 nm',
}
VERIF_ZONES = {
    'local':    (17.9, -62.8),
    'near':     (20.0, -62.8),
    'regional': (22.5, -62.5),
    'ocean':    (26.0, -61.5),
}


class User(UserMixin):
    """Modèle utilisateur Flask-Login."""
    def __init__(self, id, username, email, boat_name, boat_type, is_admin, telegram_chat_id=None):
        self.id = id
        self.username = username
        self.email = email
        self.boat_name = boat_name
        self.boat_type = boat_type
        self.is_admin = bool(is_admin)
        self.telegram_chat_id = telegram_chat_id


def get_db() -> sqlite3.Connection:
    """Ouvre une connexion SQLite avec WAL mode (résistant aux accès concurrents)."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def get_route_owned(conn: sqlite3.Connection, route_id: int, user_id: int,
                    columns: str = "*") -> sqlite3.Row | None:
    """Retourne la route si elle appartient au user, sinon None.

    Why: avant ce helper, plusieurs endpoints chargaient une route par id sans vérifier
    le user_id, permettant à un user de lire/modifier les routes d'un autre.
    How to apply: utiliser systématiquement à la place de
        SELECT ... FROM passage_routes WHERE id=?
    """
    return conn.execute(
        f"SELECT {columns} FROM passage_routes WHERE id=? AND user_id=?",
        (route_id, user_id),
    ).fetchone()


def compute_at_sea_status(conn: sqlite3.Connection, user_id: int,
                           max_age_min: int = 120, min_speed_kts: float = 1.0,
                           max_dist_nm: float = 50.0) -> dict | None:
    """Détecte si le bateau d'un user est en navigation active sur une route connue.

    Logique unifiée auparavant dupliquée dans system.api_at_sea, daily_briefing
    et certaines vues sailing. Renvoie un dict avec progression/ETA/météo, ou None
    si le bateau n'est pas en mer (avec la raison via _reason si demandé).

    Why: trois implémentations divergeaient sur les seuils et le filtrage user.
    How to apply: appeler depuis api_at_sea, daily_briefing, build_passage_summary.
    """
    import json

    pos = conn.execute(
        "SELECT timestamp, latitude, longitude, speed_knots FROM positions "
        "WHERE user_id=? AND source='inreach' ORDER BY timestamp DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if not pos:
        return None

    age_min = minutes_ago(pos["timestamp"])
    if age_min is None or age_min > max_age_min:
        return None

    speed = pos["speed_knots"] or 0
    if speed < min_speed_kts:
        return None

    lat, lon = float(pos["latitude"]), float(pos["longitude"])
    routes = conn.execute(
        "SELECT id, name, waypoints FROM passage_routes "
        "WHERE user_id=? AND status='ready' AND COALESCE(status,'')<>'archived'",
        (user_id,),
    ).fetchall()

    best_route, best_dist = None, 999.0
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

    if not best_route or best_dist > max_dist_nm:
        return None

    wps = json.loads(best_route["waypoints"])
    nearest_idx, min_wp_dist = 0, 999.0
    for i, wp in enumerate(wps):
        d = haversine_nm(lat, lon, wp["lat"], wp["lon"])
        if d < min_wp_dist:
            min_wp_dist = d
            nearest_idx = i

    total_dist = sum(
        haversine_nm(wps[i]["lat"], wps[i]["lon"], wps[i + 1]["lat"], wps[i + 1]["lon"])
        for i in range(len(wps) - 1)
    )
    dist_covered = sum(
        haversine_nm(wps[i]["lat"], wps[i]["lon"], wps[i + 1]["lat"], wps[i + 1]["lon"])
        for i in range(nearest_idx)
    )
    progress_pct = round(dist_covered / total_dist * 100) if total_dist > 0 else 0
    dist_remaining = round(
        sum(
            haversine_nm(wps[i]["lat"], wps[i]["lon"], wps[i + 1]["lat"], wps[i + 1]["lon"])
            for i in range(nearest_idx, len(wps) - 1)
        )
        + min_wp_dist,
        1,
    )

    speeds_6h = conn.execute(
        "SELECT speed_knots FROM positions WHERE user_id=? AND source='inreach' "
        "AND speed_knots > 0 AND timestamp >= datetime('now','-6 hours')",
        (user_id,),
    ).fetchall()
    avg_speed = (sum(r["speed_knots"] for r in speeds_6h) / len(speeds_6h)) if speeds_6h else speed

    eta_str, hours_remaining = None, None
    if avg_speed > 0:
        from datetime import timedelta
        hours_remaining = round(dist_remaining / avg_speed, 1)
        eta_str = (datetime.now(timezone.utc) + timedelta(hours=hours_remaining)).strftime("%d/%m %Hh%M UTC")

    return {
        "route_id": best_route["id"],
        "route_name": best_route["name"],
        "progress_pct": progress_pct,
        "distance_remaining_nm": dist_remaining,
        "eta": eta_str,
        "hours_remaining": hours_remaining,
        "actual_speed_knots": round(avg_speed, 1),
        "position": {"lat": lat, "lon": lon},
        "position_age_min": age_min,
    }


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance orthodromique en milles nautiques."""
    R = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def minutes_ago(ts_str: str | None) -> int | None:
    """Retourne le nombre de minutes écoulées depuis ts_str (ISO)."""
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
    """Génère des waypoints intermédiaires sur un arc de grand cercle."""
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
