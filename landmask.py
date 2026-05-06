"""
landmask.py — Détection terre/mer pour le fallback Python (routing.py).

Utilise le même dataset que le moteur Rust (engine/data/coastline.geojson),
indexé via shapely.STRtree pour des requêtes point-in-polygon rapides.
Le module charge le GeoJSON une seule fois (lazy, à la 1re utilisation).
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import List

from shapely.geometry import LineString, MultiPolygon, Point, Polygon, shape
from shapely.strtree import STRtree

logger = logging.getLogger(__name__)

_COASTLINE_PATH = (
    Path(__file__).resolve().parent / "engine" / "data" / "coastline.geojson"
)


@lru_cache(maxsize=1)
def _load_index() -> tuple[STRtree, list]:
    """Charge le GeoJSON, retourne (STRtree, liste de polygones)."""
    if not _COASTLINE_PATH.exists():
        logger.warning("landmask: coastline.geojson introuvable, fallback désactivé")
        return STRtree([]), []

    with _COASTLINE_PATH.open("r", encoding="utf-8") as f:
        gj = json.load(f)

    polys: List[Polygon | MultiPolygon] = []
    for feat in gj.get("features", []):
        geom = feat.get("geometry")
        if not geom:
            continue
        try:
            g = shape(geom)
        except Exception:
            continue
        if g.is_empty:
            continue
        if g.geom_type in ("Polygon", "MultiPolygon"):
            polys.append(g)

    tree = STRtree(polys)
    logger.info("landmask: %d polygones chargés", len(polys))
    return tree, polys


def is_on_land(lat: float, lon: float) -> bool:
    """True si le point (lat, lon) est sur une masse terrestre."""
    tree, polys = _load_index()
    if not polys:
        return False
    pt = Point(lon, lat)
    # STRtree.query renvoie les indices des candidats dont la bbox intersecte
    candidate_idx = tree.query(pt)
    for i in candidate_idx:
        if polys[i].contains(pt):
            return True
    return False


def crosses_land_buffered(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
    buffer_nm: float = 0.0,
) -> bool:
    """
    True si le segment (lat1,lon1)→(lat2,lon2) traverse une masse terrestre,
    en ignorant un buffer de `buffer_nm` autour de chaque extrémité.

    Le buffer permet le départ/arrivée depuis un mouillage côtier (l'endpoint
    peut être marqué "on land" selon la résolution du dataset Natural Earth 10m).
    """
    tree, polys = _load_index()
    if not polys:
        return False

    # Conversion approximative degrés↔NM (1° lat ≈ 60 NM, lon ajusté par cos(lat))
    import math

    dlat = lat2 - lat1
    dlon = (lon2 - lon1) * math.cos(math.radians(lat1))
    approx_nm = math.sqrt(dlat * dlat + dlon * dlon) * 60.0
    if approx_nm <= 0.0:
        return False
    if 2 * buffer_nm >= approx_nm:
        # Segment trop court par rapport au buffer : on considère qu'il ne traverse pas.
        return False

    # On échantillonne le segment, en ignorant les zones tampon autour des extrémités.
    n = max(2, min(60, int(math.ceil(approx_nm * 2.0))))
    t_buf = buffer_nm / approx_nm
    line = LineString([(lon1, lat1), (lon2, lat2)])
    candidate_idx = tree.query(line)
    if len(candidate_idx) == 0:
        return False

    candidates = [polys[i] for i in candidate_idx]
    for i in range(1, n):
        t = i / n
        if t < t_buf or t > 1.0 - t_buf:
            continue
        lat = lat1 + dlat * t
        lon = lon1 + (lon2 - lon1) * t
        pt = Point(lon, lat)
        for poly in candidates:
            if poly.contains(pt):
                return True
    return False


def warm_up() -> None:
    """Force le chargement de l'index (utile au démarrage worker)."""
    _load_index()
