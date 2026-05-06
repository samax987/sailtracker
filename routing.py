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
        """Charge les fichiers wind_*.json récents (30 derniers jours max)."""
        files = sorted(self.cache_dir.glob("wind_*.json"))
        if not files:
            logger.warning("GribWindProvider: aucun fichier GRIB trouvé dans %s", self.cache_dir)
            return

        # Limiter aux fichiers dont la date de validité est dans les 30 prochains jours
        # pour éviter de charger des centaines de fichiers historiques en mémoire
        cutoff_past = datetime.now(timezone.utc) - timedelta(days=1)
        cutoff_future = datetime.now(timezone.utc) + timedelta(days=30)
        skipped = 0

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

                # Sauter les fichiers hors fenêtre
                if valid_dt < cutoff_past or valid_dt > cutoff_future:
                    skipped += 1
                    continue

                with open(f, encoding='utf-8') as jf:
                    data = json.load(jf)

                self._grids[key] = self._parse_grid(data, key)
                self._timeline.append((valid_dt, key))
            except Exception as e:
                logger.debug("GribWindProvider: erreur chargement %s: %s", f.name, e)

        self._timeline.sort(key=lambda x: x[0])
        self._times = [t for t, _ in self._timeline]
        logger.info("GribWindProvider: %d fichiers GRIB chargés (%d ignorés hors fenêtre)", len(self._timeline), skipped)

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
# Détection terre (partage le dataset Natural Earth avec le moteur Rust)
try:
    import landmask  # type: ignore
except ImportError:
    landmask = None  # détection terre désactivée si module manquant


# =============================================================================
# Algorithme isochrones
# =============================================================================

def isochrone_routing(
    start: Tuple[float, float],
    end: Tuple[float, float],
    departure_dt: datetime,
    polar,
    wind_provider: GribWindProvider,
    time_step_h: float = 2.0,
    angle_step: int = 3,
    max_steps: int = 200,
    arrival_radius_nm: float = 25.0,
    max_points_per_front: int = 400,
    cell_deg: float = 0.3,
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

    # time_step_h adaptatif : routes courtes = pas plus fin pour ne pas sauter
    # par-dessus une île étroite (Saint-Martin ~7 NM). On vise ~2.5 NM par segment.
    if direct_dist < 60.0:
        time_step_h = max(0.5, min(time_step_h, direct_dist / 30.0))
        max_steps = max(max_steps, int(direct_dist * 4.0 / time_step_h) + 20)

    # arrival_radius_nm adaptatif : doit être bien plus petit que direct_dist sinon
    # le départ lui-même est dans le radius et l'algo s'arrête au step 1 sans explorer.
    arrival_radius_nm = max(2.0, min(arrival_radius_nm, direct_dist * 0.3))

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
    # Cellules déjà visitées : empêche le yo-yo (un point qui retourne à une zone
    # déjà explorée n'apporte aucune info nouvelle, donc on l'élimine).
    # Tolérance : on autorise la revisite après N steps (louvoyage légitime).
    visited_cell_deg = 0.03  # ~1.8 NM
    revisit_tolerance = 8    # nombre de steps avant de pouvoir revisiter une cellule
    visited_cells = {}  # {cell: step_de_derniere_visite}
    def _vc(pt):
        return (int(pt["lat"] / visited_cell_deg), int(pt["lon"] / visited_cell_deg))
    visited_cells[_vc(origin)] = 0
    best_arrival = None
    best_dist_remaining = direct_dist
    best_arrival_time_h = float("inf")
    arrival_found_step = None  # pour le look-ahead

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

            # Filtrer aussi le près serré impossible (TWA<30° : la polaire interpole
            # bêtement vers 0 mais aucun voilier ne peut remonter à <30° du vent).
            valid_mask = (speeds >= 0.5) & (twa_arr >= 30.0)
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
                # Filtre terre : rejeter les candidats qui tombent sur une masse terrestre
                if landmask is not None and landmask.is_on_land(lat1, lon1):
                    continue
                # Empêcher de sauter par-dessus une île : segment parent→candidat
                # ne doit pas traverser de terre (buffer 0 ici, buffer côtier au snap final)
                if landmask is not None and landmask.crosses_land_buffered(
                    lat0, lon0, lat1, lon1, buffer_nm=0.0
                ):
                    continue
                hdg = float(valid_hdgs[i])
                bs = float(valid_speeds[i])
                twa = float(valid_twa[i])
                new_pt = make_point(lat1, lon1, pt, step, hdg, bs, twa, tws)
                new_points.append(new_pt)

                d_end = haversine_nm(lat1, lon1, lat_e, lon_e)
                if d_end < arrival_radius_nm:
                    # Snap final : ne valider l'arrivée que si le segment vers end ne traverse pas la terre
                    # (buffer côtier 0.5 NM pour permettre les mouillages proches d'une île)
                    if landmask is not None and landmask.crosses_land_buffered(
                        lat1, lon1, lat_e, lon_e, buffer_nm=0.5
                    ):
                        continue
                    # Vitesse du snap final = polaire au TWA réel du segment final.
                    # Sinon l'algo croit pouvoir arriver à la vitesse du dernier step, alors qu'au près
                    # serré c'est impossible ("moment contre le vent" inacceptable à la voile).
                    snap_brg = bearing(lat1, lon1, lat_e, lon_e)
                    snap_twa = abs((twd - snap_brg + 360) % 360)
                    if snap_twa > 180:
                        snap_twa = 360 - snap_twa
                    # Refuser le snap si trop près du vent (impossible à la voile).
                    if snap_twa < 30.0:
                        continue
                    snap_speed = polar.get_boat_speed(snap_twa, tws)
                    if snap_speed < 1.0:
                        continue
                    # Critère = temps total d'arrivée (step + snap final), pas juste distance.
                    arrival_time_h = step * time_step_h + d_end / snap_speed
                    if best_arrival is None or arrival_time_h < best_arrival_time_h:
                        best_arrival = new_pt
                        best_dist_remaining = d_end
                        best_arrival_time_h = arrival_time_h

        if not new_points:
            break

        # Pruning par secteur angulaire autour de l'axe start→end
        pruned = _prune_front(new_points, lat_e, lon_e, cell_deg, max_points_per_front)
        # Filtrer les retours en arrière (cellule visitée récemment) — autorise
        # le louvoyage légitime après revisit_tolerance steps.
        def _allow(p):
            c = _vc(p)
            last = visited_cells.get(c)
            return last is None or (step - last) >= revisit_tolerance
        pruned = [p for p in pruned if _allow(p)]
        for p in pruned:
            visited_cells[_vc(p)] = step
        front = pruned

        # Stocker l'isochrone
        iso = [[p["lat"], p["lon"]] for p in front]
        all_isochrones.append(iso)

        # Look-ahead : on continue 1 step de plus après la première arrivée
        # pour comparer plusieurs points d'arrivée potentiels.
        if best_arrival is not None:
            if arrival_found_step is None:
                arrival_found_step = step
            elif step >= arrival_found_step + 1:
                break

        # Si on dépasse la limite de steps sans arrivée, prendre le point le plus proche
        if step == max_steps:
            # Prendre le point du front le plus proche de la destination
            best_arrival = min(front, key=lambda p: haversine_nm(p["lat"], p["lon"], lat_e, lon_e))

    # Backtrack pour reconstruire la route
    # Calculer le temps additionnel pour le segment final vers la destination exacte
    extra_h = 0.0
    if best_arrival is not None:
        dist_remaining = haversine_nm(best_arrival["lat"], best_arrival["lon"], lat_e, lon_e)
        if dist_remaining > 0.1:
            # Vitesse réelle du segment final selon TWA (polaire), pas vitesse du dernier step
            t_arrival = departure_dt + timedelta(hours=best_arrival["step"] * time_step_h)
            twd_f, tws_f = wind_provider.get_wind(t_arrival, best_arrival["lat"], best_arrival["lon"])
            brg_f = bearing(best_arrival["lat"], best_arrival["lon"], lat_e, lon_e)
            twa_f = abs((twd_f - brg_f + 360) % 360)
            if twa_f > 180:
                twa_f = 360 - twa_f
            final_speed = polar.get_boat_speed(twa_f, tws_f)
            if final_speed < 1.0:
                final_speed = best_arrival["speed"] if best_arrival["speed"] > 0.5 else 6.0
            extra_h = dist_remaining / final_speed

    waypoints = _backtrack(best_arrival, departure_dt, time_step_h,
                           final_dest=(lat_e, lon_e), extra_h=extra_h)

    total_steps = best_arrival["step"] if best_arrival else max_steps
    duration_h = total_steps * time_step_h + extra_h

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
    lat_e: float, lon_e: float,
    cell_deg: float,
    max_points: int,
) -> list:
    """
    Pruning par grille geographique : cellule cell_deg x cell_deg,
    garder le point le plus proche de la destination par cellule.
    Evite l'elimination des points proches de la destination (bug du pruning par secteur).
    """
    best_by_cell: Dict[tuple, dict] = {}

    for pt in points:
        cell = (
            int(pt["lat"] / cell_deg),
            int(pt["lon"] / cell_deg),
        )
        d = haversine_nm(pt["lat"], pt["lon"], lat_e, lon_e)
        cur = best_by_cell.get(cell)
        if cur is None or d < haversine_nm(cur["lat"], cur["lon"], lat_e, lon_e):
            best_by_cell[cell] = pt

    pruned = list(best_by_cell.values())

    if len(pruned) > max_points:
        pruned.sort(key=lambda p: haversine_nm(p["lat"], p["lon"], lat_e, lon_e))
        pruned = pruned[:max_points]

    return pruned


def _backtrack(arrival_pt: Optional[dict], departure_dt: datetime, time_step_h: float,
               final_dest: Optional[tuple] = None, extra_h: float = 0.0) -> list:
    """
    Remonte le chemin depuis l'arrivée jusqu'au départ.
    Si final_dest est fourni, ajoute la destination exacte comme dernier waypoint
    (le dernier point du backtrack peut être jusqu'à arrival_radius_nm de la destination).
    """
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

    # Ajouter la destination exacte si elle est différente du dernier point reconstruit
    if final_dest is not None and path:
        last = path[-1]
        lat_e, lon_e = final_dest
        dist = haversine_nm(last[0], last[1], lat_e, lon_e)
        if dist > 0.1:
            total_h = arrival_pt["step"] * time_step_h + extra_h
            eta_dest = departure_dt + timedelta(hours=total_h)
            hdg_final = bearing(last[0], last[1], lat_e, lon_e)
            path.append([
                round(lat_e, 5),
                round(lon_e, 5),
                arrival_pt["step"],
                eta_dest.isoformat(),
                round(hdg_final, 1),
                last[5],  # vitesse du dernier segment
                last[6],  # twa
                last[7],  # tws
            ])

    return path

# =============================================================================
# Grille de vent pour le moteur Rust isochrone
# =============================================================================

def build_wind_grid_for_rust(
    wind_prov: GribWindProvider,
    departure_dt: datetime,
    hours_ahead: int = 30 * 24,
    time_step_h: int = 6,
    lat_min: float = 7.0,
    lat_max: float = 26.0,
    lon_min: float = -70.0,
    lon_max: float = -14.0,
    spatial_step: float = 2.0,
) -> list:
    """
    Echantillonne GribWindProvider sur une grille reguliere et retourne
    le format wind_grid attendu par le moteur Rust (OptimizeInput.wind_grid).

    Retourne une liste de dicts :
      [{"time": "ISO", "points": [{"lat":..,"lon":..,"wind_speed_kts":..,"wind_dir_deg":..}]}, ...]
    """
    if departure_dt.tzinfo is None:
        departure_dt = departure_dt.replace(tzinfo=timezone.utc)

    lat_values = []
    lat = lat_min
    while lat <= lat_max + 0.001:
        lat_values.append(round(lat, 2))
        lat += spatial_step

    lon_values = []
    lon = lon_min
    while lon <= lon_max + 0.001:
        lon_values.append(round(lon, 2))
        lon += spatial_step

    n_pts_per_slot = len(lat_values) * len(lon_values)
    slots = []
    n_slots = max(1, int(hours_ahead / time_step_h))
    for i in range(n_slots):
        t = departure_dt + timedelta(hours=i * time_step_h)
        points = []
        for lat in lat_values:
            for lon in lon_values:
                twd, tws = wind_prov.get_wind(t, lat, lon)
                points.append({
                    "lat": lat,
                    "lon": lon,
                    "wind_speed_kts": round(float(tws), 2),
                    "wind_dir_deg": round(float(twd), 1),
                })
        slots.append({
            "time": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "points": points,
        })

    logger.info(
        "build_wind_grid_for_rust: %d slots x %d pts = %d total wind points",
        len(slots), n_pts_per_slot, len(slots) * n_pts_per_slot,
    )
    return slots



# =============================================================================
# Analyze route : estimation segment par segment d'une route MANUELLE.
# Pas d'isochrone, juste un calcul direct sur les waypoints fournis.
# Utilise la polaire calibrée pour la vitesse, le vent GRIB, et le landmask.
# =============================================================================


def _allure_from_twa(twa: float) -> str:
    """Nom français de l'allure depuis le TWA (0-180)."""
    if twa < 30:
        return "irréalisable"
    if twa < 45:
        return "près serré"
    if twa < 60:
        return "près"
    if twa < 80:
        return "près travers"
    if twa < 110:
        return "travers"
    if twa < 145:
        return "largue"
    if twa < 165:
        return "grand largue"
    return "vent arrière"


def analyze_route(
    waypoints: list,
    departure_dt: datetime,
    polar,
    wind_provider: GribWindProvider,
) -> dict:
    """
    Analyse segment par segment d'une route manuelle.

    Args:
        waypoints: liste de (lat, lon) ou de dict {lat, lon}
        departure_dt: datetime UTC de départ
        polar: polaire calibrée
        wind_provider: GribWindProvider

    Returns:
        {
          "segments": [{from, to, dist_nm, brg, twd, tws, twa, allure,
                        boat_speed_kts, duration_h, eta_start, eta_end, warnings}, ...],
          "total": {distance_nm, duration_h, eta, avg_speed_kts, departure}
        }
    """
    if departure_dt.tzinfo is None:
        departure_dt = departure_dt.replace(tzinfo=timezone.utc)

    # Normaliser : accepter dicts ou tuples
    pts = []
    for wp in waypoints:
        if isinstance(wp, dict):
            pts.append((float(wp["lat"]), float(wp["lon"])))
        else:
            pts.append((float(wp[0]), float(wp[1])))

    if len(pts) < 2:
        return {
            "segments": [],
            "total": {"distance_nm": 0.0, "duration_h": 0.0,
                      "eta": departure_dt.isoformat(),
                      "avg_speed_kts": 0.0,
                      "departure": departure_dt.isoformat()},
        }

    # Tente d'importer landmask pour les warnings côte (best-effort)
    try:
        import landmask as _lm
    except Exception:
        _lm = None

    segments = []
    t_cursor = departure_dt
    total_dist = 0.0

    for i in range(len(pts) - 1):
        lat1, lon1 = pts[i]
        lat2, lon2 = pts[i + 1]
        dist_nm = haversine_nm(lat1, lon1, lat2, lon2)
        brg = bearing(lat1, lon1, lat2, lon2)

        # Vent : on prend au midpoint à mi-temps estimé.
        # On fait deux passes : 1) estimer durée avec vent au point de départ,
        # 2) recalculer au midpoint à mi-durée.
        twd0, tws0 = wind_provider.get_wind(t_cursor, lat1, lon1)
        twa0 = abs((twd0 - brg + 360) % 360)
        if twa0 > 180:
            twa0 = 360 - twa0
        bs0 = polar.get_boat_speed(twa0, tws0) if twa0 >= 30.0 else 0.0
        rough_dur_h = dist_nm / bs0 if bs0 > 0.5 else dist_nm / 4.0

        t_mid = t_cursor + timedelta(hours=rough_dur_h / 2)
        mid_lat = (lat1 + lat2) / 2
        mid_lon = (lon1 + lon2) / 2
        twd, tws = wind_provider.get_wind(t_mid, mid_lat, mid_lon)
        twa = abs((twd - brg + 360) % 360)
        if twa > 180:
            twa = 360 - twa

        # Vitesse polaire au TWA réel. Si TWA<30°, irréalisable à la voile.
        if twa >= 30.0:
            bs = polar.get_boat_speed(twa, tws)
        else:
            bs = 0.0

        # Si vraiment irréalisable (près trop serré ou vent < 2kt), durée infinie.
        if bs < 0.5 or tws < 2.0:
            duration_h = float("inf")
            eta_end = None
        else:
            duration_h = dist_nm / bs
            eta_end = t_cursor + timedelta(hours=duration_h)

        warnings = []
        if twa < 30.0:
            warnings.append("près_irréalisable")
        elif twa < 40.0:
            warnings.append("près_très_serré")
        if twa > 165.0:
            warnings.append("vent_arrière_instable")
        if tws > 25.0:
            warnings.append("vent_fort")
        if tws < 4.0:
            warnings.append("vent_faible")
        # Frôle la côte (segment traverse terre selon landmask, buffer 0)
        if _lm is not None:
            try:
                if _lm.crosses_land_buffered(lat1, lon1, lat2, lon2, buffer_nm=0.0):
                    warnings.append("traverse_terre")
            except Exception:
                pass

        seg = {
            "from": [round(lat1, 5), round(lon1, 5)],
            "to": [round(lat2, 5), round(lon2, 5)],
            "dist_nm": round(dist_nm, 2),
            "brg": round(brg, 0),
            "twd": round(twd, 0),
            "tws": round(tws, 1),
            "twa": round(twa, 0),
            "allure": _allure_from_twa(twa),
            "boat_speed_kts": round(bs, 2),
            "duration_h": round(duration_h, 2) if duration_h != float("inf") else None,
            "eta_start": t_cursor.isoformat(),
            "eta_end": eta_end.isoformat() if eta_end else None,
            "warnings": warnings,
        }
        segments.append(seg)

        total_dist += dist_nm
        if eta_end:
            t_cursor = eta_end
        else:
            # Si segment irréalisable, on continue quand même mais ETA totale invalide
            t_cursor = t_cursor + timedelta(hours=24)

    # Total
    total_duration_h = sum(
        s["duration_h"] for s in segments if s["duration_h"] is not None
    )
    has_unrealisable = any(s["duration_h"] is None for s in segments)
    avg_speed = total_dist / total_duration_h if total_duration_h > 0 else 0.0

    return {
        "segments": segments,
        "total": {
            "distance_nm": round(total_dist, 2),
            "duration_h": round(total_duration_h, 2),
            "eta": t_cursor.isoformat() if not has_unrealisable else None,
            "avg_speed_kts": round(avg_speed, 2),
            "departure": departure_dt.isoformat(),
            "has_unrealisable_segment": has_unrealisable,
        },
    }
