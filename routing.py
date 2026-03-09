#!/usr/bin/env python3
"""
routing.py — Algorithme de routage par isochrones pour SailTracker
"""

import bisect
import json
import numpy as np
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("sailtracker_server")

BASE_DIR = Path(__file__).parent
GRIB_CACHE_DIR = BASE_DIR / "static" / "grib_cache"


# =============================================================================
# Géodésie
# =============================================================================

EARTH_RADIUS_NM = 3440.065


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    return EARTH_RADIUS_NM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Retourne le cap initial (°) de (lat1,lon1) vers (lat2,lon2)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def move_point(lat: float, lon: float, bearing_deg: float, dist_nm: float) -> Tuple[float, float]:
    """Déplace un point de dist_nm nœuds sur un cap donné (sphère)."""
    d = dist_nm / EARTH_RADIUS_NM
    b = math.radians(bearing_deg)
    phi1 = math.radians(lat)
    lam1 = math.radians(lon)
    phi2 = math.asin(math.sin(phi1) * math.cos(d) + math.cos(phi1) * math.sin(d) * math.cos(b))
    lam2 = lam1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(phi1),
                              math.cos(d) - math.sin(phi1) * math.sin(phi2))
    return math.degrees(phi2), math.degrees(lam2)


def twa_from_hdg_twd(hdg: float, twd: float) -> float:
    """Angle au vent vrai (0-180°) depuis le cap et la direction du vent."""
    angle = (twd - hdg + 360) % 360
    if angle > 180:
        angle = 360 - angle
    return angle


# =============================================================================
# Fournisseur de vent GRIB
# =============================================================================

class GribWindProvider:
    """
    Charge les fichiers JSON du cache GRIB et fournit vent (twd, tws)
    pour n'importe quel point/moment par interpolation nearest-neighbor + linéaire.
    """

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or GRIB_CACHE_DIR
        self._grids: Dict[str, dict] = {}  # key: "YYYYMMDD_HHz_fFFF" → parsed grid
        self._timeline: List[Tuple[datetime, str]] = []  # sorted list of (dt, key)
        self._load_all()

    def _load_all(self):
        """Charge tous les fichiers wind_*.json disponibles."""
        files = sorted(self.cache_dir.glob("wind_*.json"))
        if not files:
            logger.warning("GribWindProvider: aucun fichier GRIB trouvé dans %s", self.cache_dir)
            return

        for f in files:
            try:
                key = f.stem  # e.g. "wind_20260305_00z_f006"
                parts = key.split("_")
                # format: wind_YYYYMMDD_HHz_fFFF
                date_str = parts[1]        # "20260305"
                run_str = parts[2]         # "00z"
                fcast_str = parts[3]       # "f006"
                run_h = int(run_str.replace("z", ""))
                fcast_h = int(fcast_str[1:])
                ref_dt = datetime(
                    int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]),
                    run_h, 0, 0, tzinfo=timezone.utc
                )
                valid_dt = ref_dt + timedelta(hours=fcast_h)

                with open(f, encoding='utf-8') as jf:
                    data = json.load(jf)

                self._grids[key] = self._parse_grid(data, key)
                self._timeline.append((valid_dt, key))
            except Exception as e:
                logger.debug("GribWindProvider: erreur chargement %s: %s", f.name, e)

        self._timeline.sort(key=lambda x: x[0])
        self._times = [t for t, _ in self._timeline]
        logger.info("GribWindProvider: %d fichiers GRIB chargés", len(self._timeline))

    def _parse_grid(self, data: list, key: str) -> dict:
        """Parse le JSON format leaflet-velocity (2 composantes U et V)."""
        u_comp = v_comp = None
        for comp in data:
            hdr = comp.get("header", {})
            pcat = hdr.get("parameterCategory", -1)
            pnum = hdr.get("parameterNumber", -1)
            if pcat == 2 and pnum == 2:
                u_comp = comp
            elif pcat == 2 and pnum == 3:
                v_comp = comp

        if u_comp is None or v_comp is None:
            raise ValueError(f"Composantes U/V non trouvées dans {key}")

        hdr = u_comp["header"]
        return {
            "la1": hdr["la1"], "lo1": hdr["lo1"],
            "la2": hdr["la2"], "lo2": hdr["lo2"],
            "dx": hdr["dx"], "dy": hdr["dy"],
            "nx": hdr["nx"], "ny": hdr["ny"],
            "u": u_comp["data"],
            "v": v_comp["data"],
        }

    def _get_uv(self, grid: dict, lat: float, lon: float) -> Tuple[float, float]:
        """Nearest-neighbor dans la grille."""
        # Wrap longitude
        lo1, lo2 = grid["lo1"], grid["lo2"]
        dx, dy = grid["dx"], grid["dy"]
        nx, ny = grid["nx"], grid["ny"]
        la1 = grid["la1"]

        # Clamp lat/lon
        lat_c = max(min(lat, max(grid["la1"], grid["la2"])),
                    min(grid["la1"], grid["la2"]))
        lon_c = lon
        while lon_c < lo1:
            lon_c += 360
        while lon_c > lo2:
            lon_c -= 360

        ix = int(round((lon_c - lo1) / dx))
        iy = int(round((la1 - lat_c) / dy))
        ix = max(0, min(nx - 1, ix))
        iy = max(0, min(ny - 1, iy))

        idx = iy * nx + ix
        u = grid["u"][idx] if idx < len(grid["u"]) else 0.0
        v = grid["v"][idx] if idx < len(grid["v"]) else 0.0
        return (u or 0.0), (v or 0.0)

    def get_wind(self, dt: datetime, lat: float, lon: float) -> Tuple[float, float]:
        """
        Retourne (twd_deg, tws_kts) pour un point et un moment.
        Interpolation temporelle linéaire entre les deux timesteps les plus proches.
        """
        if not self._timeline:
            return 0.0, 0.0

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        # Trouver les 2 timesteps encadrant dt (O(log n) avec bisect)
        i = bisect.bisect_right(self._times, dt)
        if i == 0:
            _, key = self._timeline[0]
            return self._uv_to_wind(self._get_uv(self._grids[key], lat, lon))
        if i >= len(self._timeline):
            _, key = self._timeline[-1]
            return self._uv_to_wind(self._get_uv(self._grids[key], lat, lon))
        t0, k0 = self._timeline[i - 1]
        t1, k1 = self._timeline[i]
        u0, v0 = self._get_uv(self._grids[k0], lat, lon)
        u1, v1 = self._get_uv(self._grids[k1], lat, lon)

        # Interpolation linéaire
        total = (t1 - t0).total_seconds()
        if total == 0:
            alpha = 0.0
        else:
            alpha = (dt - t0).total_seconds() / total
        u = u0 + alpha * (u1 - u0)
        v = v0 + alpha * (v1 - v0)
        return self._uv_to_wind((u, v))

    @staticmethod
    def _uv_to_wind(uv: Tuple[float, float]) -> Tuple[float, float]:
        """Convertit composantes U/V météo en (twd°, tws_kts)."""
        u, v = uv
        speed_ms = math.sqrt(u ** 2 + v ** 2)
        tws_kts = speed_ms * 1.94384  # m/s → noeuds
        # Convention météo : vent vient de... (inverse du vecteur)
        twd = (270 - math.degrees(math.atan2(v, u))) % 360
        return twd, tws_kts


# =============================================================================
# Algorithme isochrones
# =============================================================================

def isochrone_routing(
    start: Tuple[float, float],
    end: Tuple[float, float],
    departure_dt: datetime,
    polar,
    wind_provider: GribWindProvider,
    time_step_h: float = 3.0,
    angle_step: int = 5,
    max_steps: int = 200,
    arrival_radius_nm: float = 15.0,
    max_points_per_front: int = 300,
    sector_deg: float = 3.0,
) -> dict:
    """
    Routage par isochrones.

    Returns:
        {
            "waypoints": [[lat, lon, iso_step, eta_iso, hdg, speed_kts, twa, tws], ...],
            "isochrones": [[[lat, lon], ...], ...],  # une liste par pas de temps
            "stats": {distance_nm, duration_h, steps, ...},
            "direct_distance_nm": float,
        }
    """
    if departure_dt.tzinfo is None:
        departure_dt = departure_dt.replace(tzinfo=timezone.utc)

    lat_s, lon_s = start
    lat_e, lon_e = end
    direct_dist = haversine_nm(lat_s, lon_s, lat_e, lon_e)
    direct_bearing = bearing(lat_s, lon_s, lat_e, lon_e)

    # Structure d'un point du front
    # { lat, lon, parent, step, hdg, speed_kts, twa, tws, dist_from_start }
    def make_point(lat, lon, parent, step, hdg, speed, twa, tws):
        return {
            "lat": lat, "lon": lon,
            "parent": parent,
            "step": step,
            "hdg": hdg, "speed": speed,
            "twa": twa, "tws": tws,
        }

    # Front initial
    origin = make_point(lat_s, lon_s, None, 0, 0, 0, 0, 0)
    front = [origin]
    all_isochrones = []
    best_arrival = None
    best_dist_remaining = direct_dist

    angles = list(range(0, 360, angle_step))

    for step in range(1, max_steps + 1):
        t_current = departure_dt + timedelta(hours=(step - 1) * time_step_h)
        t_next = departure_dt + timedelta(hours=step * time_step_h)

        new_points = []

        t_mid = t_current + timedelta(hours=time_step_h / 2)
        angles_arr = np.array(angles, dtype=float)
        angles_rad = np.radians(angles_arr)

        for pt in front:
            lat0, lon0 = pt["lat"], pt["lon"]

            # Vent calculé une seule fois par point
            twd, tws = wind_provider.get_wind(t_mid, lat0, lon0)
            if tws < 2.0:
                continue

            # Vectoriser sur tous les caps en une seule passe
            twa_arr = np.abs((twd - angles_arr + 360) % 360)
            twa_arr = np.where(twa_arr > 180, 360 - twa_arr, twa_arr)
            speeds = polar.get_boat_speeds_batch(twa_arr, tws)

            valid_mask = speeds >= 0.5
            if not np.any(valid_mask):
                continue

            valid_hdgs = angles_arr[valid_mask]
            valid_twa = twa_arr[valid_mask]
            valid_speeds = speeds[valid_mask]
            valid_dists = valid_speeds * time_step_h

            # move_point vectorisé
            d_rad = valid_dists / EARTH_RADIUS_NM
            b_rad = np.radians(valid_hdgs)
            phi1 = math.radians(lat0)
            lam1 = math.radians(lon0)
            phi2 = np.arcsin(math.sin(phi1) * np.cos(d_rad) +
                             math.cos(phi1) * np.sin(d_rad) * np.cos(b_rad))
            lam2 = lam1 + np.arctan2(np.sin(b_rad) * np.sin(d_rad) * math.cos(phi1),
                                     np.cos(d_rad) - math.sin(phi1) * np.sin(phi2))
            lats1 = np.degrees(phi2)
            lons1 = np.degrees(lam2)

            for i in range(len(valid_hdgs)):
                lat1, lon1 = float(lats1[i]), float(lons1[i])
                hdg = float(valid_hdgs[i])
                bs = float(valid_speeds[i])
                twa = float(valid_twa[i])
                new_pt = make_point(lat1, lon1, pt, step, hdg, bs, twa, tws)
                new_points.append(new_pt)

                d_end = haversine_nm(lat1, lon1, lat_e, lon_e)
                if d_end < arrival_radius_nm:
                    if best_arrival is None or d_end < best_dist_remaining:
                        best_arrival = new_pt
                        best_dist_remaining = d_end

        if not new_points:
            break

        # Pruning par secteur angulaire autour de l'axe start→end
        pruned = _prune_front(new_points, lat_s, lon_s, lat_e, lon_e,
                               sector_deg, max_points_per_front)
        front = pruned

        # Stocker l'isochrone
        iso = [[p["lat"], p["lon"]] for p in front]
        all_isochrones.append(iso)

        if best_arrival is not None:
            break

        # Si on dépasse la limite de steps sans arrivée, prendre le point le plus proche
        if step == max_steps:
            # Prendre le point du front le plus proche de la destination
            best_arrival = min(front, key=lambda p: haversine_nm(p["lat"], p["lon"], lat_e, lon_e))

    # Backtrack pour reconstruire la route
    waypoints = _backtrack(best_arrival, departure_dt, time_step_h)

    total_steps = best_arrival["step"] if best_arrival else max_steps
    duration_h = total_steps * time_step_h

    return {
        "waypoints": waypoints,
        "isochrones": all_isochrones,
        "stats": {
            "direct_distance_nm": round(direct_dist, 1),
            "duration_h": round(duration_h, 1),
            "steps": total_steps,
            "time_step_h": time_step_h,
            "departure": departure_dt.isoformat(),
            "eta": (departure_dt + timedelta(hours=duration_h)).isoformat(),
        },
    }


def _prune_front(
    points: list,
    lat_s: float, lon_s: float,
    lat_e: float, lon_e: float,
    sector_deg: float,
    max_points: int,
) -> list:
    """
    Pruning : par secteur de sector_deg° autour du cap start→end,
    garder le point le plus avancé (distance depuis start la plus grande).
    """
    n_sectors = int(360 / sector_deg)
    sectors: Dict[int, dict] = {}

    for pt in points:
        # Angle de ce point vu depuis le start
        b = bearing(lat_s, lon_s, pt["lat"], pt["lon"])
        sector_id = int(b / sector_deg) % n_sectors
        dist = haversine_nm(lat_s, lon_s, pt["lat"], pt["lon"])

        if sector_id not in sectors or dist > haversine_nm(lat_s, lon_s,
                                                            sectors[sector_id]["lat"],
                                                            sectors[sector_id]["lon"]):
            sectors[sector_id] = pt

    pruned = list(sectors.values())

    # Si trop de points, garder les max_points les plus proches de la destination
    if len(pruned) > max_points:
        pruned.sort(key=lambda p: haversine_nm(p["lat"], p["lon"], lat_e, lon_e))
        pruned = pruned[:max_points]

    return pruned


def _backtrack(arrival_pt: Optional[dict], departure_dt: datetime, time_step_h: float) -> list:
    """Remonte le chemin depuis l'arrivée jusqu'au départ."""
    if arrival_pt is None:
        return []

    path = []
    pt = arrival_pt
    while pt is not None:
        eta = departure_dt + timedelta(hours=pt["step"] * time_step_h)
        path.append([
            round(pt["lat"], 5),
            round(pt["lon"], 5),
            pt["step"],
            eta.isoformat(),
            round(pt["hdg"], 1),
            round(pt["speed"], 2),
            round(pt["twa"], 1),
            round(pt["tws"], 1),
        ])
        pt = pt["parent"]

    path.reverse()
    return path
