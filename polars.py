#!/usr/bin/env python3
"""
polars.py — Gestion des polaires de vitesse POLLEN 1
"""

import csv
import logging
from pathlib import Path

import numpy as np
from scipy.interpolate import RegularGridInterpolator

logger = logging.getLogger("sailtracker_server")

BASE_DIR = Path(__file__).parent
DEFAULT_POLAR_PATH = BASE_DIR / "data" / "polars" / "pollen1.csv"


class PolarDiagram:
    """Diagramme polaire avec interpolation bilinéaire (scipy)."""

    def __init__(self, filepath=None):
        self.filepath = Path(filepath) if filepath else DEFAULT_POLAR_PATH
        self._twa_arr = None  # angles TWA en degrés
        self._tws_arr = None  # vitesses TWS en nœuds
        self._speeds = None   # matrice vitesses (twa × tws)
        self._interp = None
        self._load()

    def _load(self):
        """Charge le CSV et construit l'interpolateur."""
        twa_list = []
        speed_rows = []

        with open(self.filepath, newline='', encoding='utf-8') as f:
            reader = csv.reader(f, delimiter=';')
            header = next(reader)
            # Colonnes TWS (skip premier élément "TWA/TWS")
            tws_list = [float(v) for v in header[1:]]

            for row in reader:
                if not row or not row[0].strip():
                    continue
                twa_list.append(float(row[0]))
                speed_rows.append([float(v) for v in row[1:]])

        self._twa_arr = np.array(twa_list, dtype=float)
        self._tws_arr = np.array(tws_list, dtype=float)
        self._speeds = np.array(speed_rows, dtype=float)  # shape (n_twa, n_tws)
        self._build_interp()

    def _build_interp(self):
        self._interp = RegularGridInterpolator(
            (self._twa_arr, self._tws_arr),
            self._speeds,
            method='linear',
            bounds_error=False,
            fill_value=None  # extrapolation
        )

    def get_boat_speed(self, twa: float, tws: float) -> float:
        """Retourne la vitesse bateau en nœuds pour un TWA/TWS donné."""
        twa = max(0.0, min(180.0, abs(float(twa))))
        tws = max(0.0, float(tws))
        result = float(self._interp([[twa, tws]])[0])
        return max(0.0, result)

    def get_boat_speeds_batch(self, twa_arr, tws: float):
        """Batch lookup: array of TWA values at a fixed TWS. Returns np.ndarray."""
        twa_c = np.clip(np.abs(twa_arr), 0.0, 180.0)
        tws_val = max(0.0, float(tws))
        pts = np.column_stack([twa_c, np.full(len(twa_c), tws_val, dtype=float)])
        return np.maximum(0.0, self._interp(pts))

    def to_dict(self) -> dict:
        """Retourne les polaires au format JSON."""
        return {
            "twa": self._twa_arr.tolist(),
            "tws": self._tws_arr.tolist(),
            "speeds": self._speeds.tolist(),
        }

    def save(self, filepath=None):
        """Sauvegarde les polaires en CSV."""
        path = Path(filepath) if filepath else self.filepath
        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f, delimiter=';')
            header = ["TWA/TWS"] + [str(int(v)) for v in self._tws_arr]
            writer.writerow(header)
            for i, twa in enumerate(self._twa_arr):
                row = [str(int(twa))] + [f"{v:.2f}" for v in self._speeds[i]]
                writer.writerow(row)

    def update_speed(self, twa: float, tws: float, speed: float):
        """Met à jour une valeur et reconstruit l'interpolateur."""
        # Trouver l'index le plus proche
        twa_idx = int(np.argmin(np.abs(self._twa_arr - twa)))
        tws_idx = int(np.argmin(np.abs(self._tws_arr - tws)))
        self._speeds[twa_idx, tws_idx] = max(0.0, float(speed))
        self._build_interp()

    def get_twa_range(self):
        return self._twa_arr.tolist()

    def get_tws_range(self):
        return self._tws_arr.tolist()


def update_polars_from_observations(db, polar: PolarDiagram, min_obs: int = 5) -> int:
    """
    Calibre les polaires en blendant les observations réelles.
    Retourne le nombre de cases mises à jour.
    """
    c = db.cursor()

    # Récupère toutes les observations valides
    try:
        rows = c.execute("""
            SELECT twa_deg, tws_kts, stw_kts
            FROM polar_observations
            WHERE is_valid = 1
              AND twa_deg IS NOT NULL
              AND tws_kts IS NOT NULL
              AND stw_kts IS NOT NULL
        """).fetchall()
    except Exception as e:
        logger.warning("polar_observations table not ready: %s", e)
        return 0

    if len(rows) < min_obs:
        return 0

    twa_bins = polar.get_twa_range()
    tws_bins = polar.get_tws_range()
    updated = 0

    for twa_center in twa_bins:
        for tws_center in tws_bins:
            # Sélectionne les observations dans la case ±7.5° / ±2kts
            cell_stws = [
                row[2] for row in rows
                if abs(row[0] - twa_center) <= 7.5 and abs(row[1] - tws_center) <= 2.0
            ]
            n = len(cell_stws)
            if n < min_obs:
                continue

            observed_median = float(np.median(cell_stws))
            theoretical = polar.get_boat_speed(twa_center, tws_center)

            # Blend progressif : 5 obs → 30% réel, 50+ obs → 90% réel
            blend = min(0.9, 0.3 + 0.6 * (n - min_obs) / 45.0)
            blended = (1 - blend) * theoretical + blend * observed_median
            polar.update_speed(twa_center, tws_center, blended)
            updated += 1

    if updated > 0:
        polar.save()
        logger.info("Polaires calibrées : %d cases mises à jour", updated)

    return updated


# Singleton partagé
_polar_instance = None


def get_polar() -> PolarDiagram:
    global _polar_instance
    if _polar_instance is None:
        _polar_instance = PolarDiagram()
        logger.info("PolarDiagram chargé depuis %s", _polar_instance.filepath)
    return _polar_instance


def reload_polar():
    global _polar_instance
    _polar_instance = PolarDiagram()
    return _polar_instance
