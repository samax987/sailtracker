#!/usr/bin/env python3
"""
polars.py — Gestion des polaires de vitesse POLLEN 1
"""

import csv
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.interpolate import RegularGridInterpolator

logger = logging.getLogger("sailtracker_server")

BASE_DIR = Path(__file__).parent
DEFAULT_POLAR_PATH = BASE_DIR / "data" / "polars" / "pollen1.csv"
DB_PATH = BASE_DIR / "sailtracker.db"


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

    @classmethod
    def load_from_db(cls, db_path=None) -> 'PolarDiagram':
        """Charge le polaire depuis polar_matrix SQLite."""
        path = db_path or DB_PATH
        conn = sqlite3.connect(path)
        rows = conn.execute(
            "SELECT twa_deg, tws_kts, speed_kts FROM polar_matrix ORDER BY twa_deg, tws_kts"
        ).fetchall()
        conn.close()

        if not rows:
            raise ValueError("polar_matrix est vide")

        # Extraire les valeurs uniques triées
        twa_set = sorted(set(r[0] for r in rows))
        tws_set = sorted(set(r[1] for r in rows))

        twa_arr = np.array(twa_set, dtype=float)
        tws_arr = np.array(tws_set, dtype=float)
        speeds = np.zeros((len(twa_set), len(tws_set)), dtype=float)

        twa_idx = {v: i for i, v in enumerate(twa_set)}
        tws_idx = {v: i for i, v in enumerate(tws_set)}

        for twa, tws, spd in rows:
            speeds[twa_idx[twa], tws_idx[tws]] = spd

        obj = cls.__new__(cls)
        obj.filepath = DEFAULT_POLAR_PATH
        obj._twa_arr = twa_arr
        obj._tws_arr = tws_arr
        obj._speeds = speeds
        obj._interp = None
        obj._build_interp()
        return obj

    def save_to_db(self, db_path=None):
        """Sauvegarde les polaires dans polar_matrix (UPSERT)."""
        path = db_path or DB_PATH
        conn = sqlite3.connect(path)
        now = datetime.utcnow().isoformat()
        for i, twa in enumerate(self._twa_arr):
            for j, tws in enumerate(self._tws_arr):
                conn.execute("""
                    INSERT INTO polar_matrix (twa_deg, tws_kts, speed_kts, calibrated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(twa_deg, tws_kts) DO UPDATE SET
                        speed_kts = excluded.speed_kts,
                        calibrated_at = excluded.calibrated_at
                """, (float(twa), float(tws), float(self._speeds[i][j]), now))
        conn.commit()
        conn.close()

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
        """Sauvegarde les polaires en CSV (backup/export)."""
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


def update_polars_from_observations(db, polar: PolarDiagram, min_obs: int = 5,
                                    full_sail_only: bool = True) -> int:
    """
    Calibre les polaires en blendant les observations réelles.
    full_sail_only=True (défaut) : n'utilise que les observations sans réduction de voile
    (reef_count=0 ET genoa_pct>=90) pour la polaire de référence.
    Retourne le nombre de cases mises à jour.
    Écrit les cases calibrées dans polar_matrix (source de vérité).
    """
    c = db.cursor()

    # Filtre full sail : observations sans config, ou config plein voile
    full_sail_filter = ""
    if full_sail_only:
        full_sail_filter = """
              AND (po.sail_config_id IS NULL
                   OR EXISTS (
                       SELECT 1 FROM sail_config_periods sc
                       WHERE sc.id = po.sail_config_id
                         AND sc.reef_count = 0
                         AND sc.genoa_pct >= 90
                         AND sc.spinnaker = 0
                   ))
        """

    # Récupère les observations valides (plein voile uniquement si demandé)
    try:
        rows = c.execute(f"""
            SELECT po.twa_deg, po.tws_kts, po.stw_kts
            FROM polar_observations po
            WHERE po.is_valid = 1
              AND po.twa_deg IS NOT NULL
              AND po.tws_kts IS NOT NULL
              AND po.stw_kts IS NOT NULL
              {full_sail_filter}
        """).fetchall()
    except Exception as e:
        logger.warning("polar_observations table not ready: %s", e)
        return 0

    if len(rows) < min_obs:
        return 0

    twa_bins = polar.get_twa_range()
    tws_bins = polar.get_tws_range()
    updated = 0
    now = datetime.utcnow().isoformat()

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

            # UPSERT dans polar_matrix (source de vérité)
            c.execute("""
                INSERT INTO polar_matrix (twa_deg, tws_kts, speed_kts, n_obs, calibrated_at, source)
                VALUES (?, ?, ?, ?, ?, 'calibrated')
                ON CONFLICT(twa_deg, tws_kts) DO UPDATE SET
                    speed_kts = excluded.speed_kts,
                    n_obs = excluded.n_obs,
                    calibrated_at = excluded.calibrated_at,
                    source = 'calibrated'
            """, (twa_center, tws_center, blended, n, now))
            updated += 1

    if updated > 0:
        db.commit()
        polar.save()  # CSV backup/export seulement
        mode = "plein voile uniquement" if full_sail_only else "toutes configs"
        logger.info("Polaires calibrées (%s) : %d cases sur %d observations", mode, updated, len(rows))

    return updated


def get_polar(db_path=None) -> PolarDiagram:
    """Charge le polaire depuis DB à chaque appel (pas de singleton)."""
    path = db_path or DB_PATH
    try:
        return PolarDiagram.load_from_db(path)
    except Exception as e:
        logger.warning("Fallback CSV (DB indisponible: %s)", e)
        return PolarDiagram()


def reload_polar():
    return get_polar()
