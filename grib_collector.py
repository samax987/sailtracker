#!/usr/bin/env python3
"""
grib_collector.py — Collecte et cache des données GFS GRIB2 pour l'overlay leaflet-velocity.
Télécharge les données de vent U/V à 10m depuis NOMADS pour la région Atlantique.
Lance via cron : 30 3,9,15,21 * * *
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

BASE_DIR = Path(__file__).parent
GRIB_CACHE_DIR = BASE_DIR / "static" / "grib_cache"
LOG_DIR = BASE_DIR / "logs"

# Région Atlantique (Cap-Vert → Caraïbes)
REGION = {
    "leftlon": -85, "rightlon": 5,
    "toplat": 35, "bottomlat": -5,
}

# Horizons à télécharger (heures)
FORECAST_HOURS = [0, 6, 12, 18, 24, 30, 36, 42, 48, 60, 72]

# Garder 2 runs au maximum
MAX_RUNS = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "grib_cron.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("grib_collector")


def find_latest_gfs_run():
    """Trouve le dernier run GFS disponible (00/06/12/18z, dispo ~3h30 après)."""
    now_utc = datetime.now(timezone.utc)
    # Les runs sont disponibles environ 3h30 après l'heure nominale
    delay_hours = 3.5
    available_time = now_utc - timedelta(hours=delay_hours)
    run_hour = (available_time.hour // 6) * 6
    run_date = available_time.replace(hour=run_hour, minute=0, second=0, microsecond=0)
    if run_date > available_time:
        run_date -= timedelta(hours=6)
    return run_date


def download_grib_file(run_dt, fh_hours, tmp_path):
    """Télécharge un fichier GRIB2 filtré depuis NOMADS."""
    date_str = run_dt.strftime("%Y%m%d")
    hh = run_dt.strftime("%H")
    fh_str = str(fh_hours).zfill(3)

    url = (
        f"https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
        f"?file=gfs.t{hh}z.pgrb2.0p25.f{fh_str}"
        f"&var_UGRD=on&var_VGRD=on&lev_10_m_above_ground=on"
        f"&leftlon={REGION['leftlon']}&rightlon={REGION['rightlon']}"
        f"&toplat={REGION['toplat']}&bottomlat={REGION['bottomlat']}"
        f"&dir=%2Fgfs.{date_str}%2F{hh}%2Fatmos"
    )

    try:
        logger.info("Téléchargement f%s : %s", fh_str, url[:80] + "...")
        resp = requests.get(url, timeout=120, stream=True)
        resp.raise_for_status()
        with open(tmp_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        size_kb = tmp_path.stat().st_size // 1024
        logger.info("Téléchargé f%s : %d KB", fh_str, size_kb)
        return True
    except requests.exceptions.HTTPError as e:
        logger.warning("HTTP error f%s : %s", fh_str, e)
        return False
    except Exception as e:
        logger.error("Erreur téléchargement f%s : %s", fh_str, e)
        return False


def parse_grib_to_velocity_json(grib_path, run_dt, fh_hours):
    """Parse le GRIB2 et retourne le JSON leaflet-velocity."""
    try:
        import cfgrib
        import xarray as xr
    except ImportError:
        logger.error("cfgrib/xarray non disponible")
        return None

    try:
        datasets = cfgrib.open_datasets(str(grib_path))
    except Exception as e:
        logger.error("Erreur ouverture GRIB %s : %s", grib_path, e)
        return None

    u10 = None
    v10 = None

    for ds in datasets:
        if 'u10' in ds:
            u10 = ds['u10']
        if 'v10' in ds:
            v10 = ds['v10']
        # Parfois nommés différemment
        for var in ds.data_vars:
            vl = var.lower()
            if 'u' in vl and '10' in str(ds[var].attrs.get('GRIB_shortName', '')):
                u10 = ds[var]
            if 'v' in vl and '10' in str(ds[var].attrs.get('GRIB_shortName', '')):
                v10 = ds[var]

    if u10 is None or v10 is None:
        # Try by GRIB typeOfLevel
        for ds in datasets:
            attrs = {}
            for var in ds.data_vars:
                a = ds[var].attrs
                if a.get('GRIB_shortName') == '10u':
                    u10 = ds[var]
                elif a.get('GRIB_shortName') == '10v':
                    v10 = ds[var]

    if u10 is None or v10 is None:
        logger.error("U10/V10 non trouvés dans le fichier GRIB")
        return None

    # Extract grid info
    lats = u10.coords.get('latitude', u10.coords.get('lat'))
    lons = u10.coords.get('longitude', u10.coords.get('lon'))

    if lats is None or lons is None:
        logger.error("Coordonnées lat/lon non trouvées")
        return None

    lat_vals = lats.values
    lon_vals = lons.values

    # Ensure descending latitudes (north to south for leaflet-velocity)
    if lat_vals[0] < lat_vals[-1]:
        u_arr = np.flipud(u10.values.squeeze())
        v_arr = np.flipud(v10.values.squeeze())
        lat_vals = lat_vals[::-1]
    else:
        u_arr = u10.values.squeeze()
        v_arr = v10.values.squeeze()

    # Handle longitude wrap: convert 0-360 → -180/+180 BEFORE cropping
    if lon_vals[0] > 180 or (len(lon_vals) > 1 and lon_vals[-1] > 180):
        lon_vals = np.where(lon_vals > 180, lon_vals - 360, lon_vals)
        # Re-sort west→east after conversion
        sort_idx = np.argsort(lon_vals)
        lon_vals = lon_vals[sort_idx]
        u_arr = u_arr[:, sort_idx]
        v_arr = v_arr[:, sort_idx]

    # Crop to Atlantic region (NOMADS filter may still return global grid)
    lat_mask = (lat_vals >= REGION['bottomlat'] - 0.1) & (lat_vals <= REGION['toplat'] + 0.1)
    lon_mask = (lon_vals >= REGION['leftlon'] - 0.1) & (lon_vals <= REGION['rightlon'] + 0.1)
    if lat_mask.sum() > 10 and lon_mask.sum() > 10:
        lat_idx = np.where(lat_mask)[0]
        lon_idx = np.where(lon_mask)[0]
        u_arr = u_arr[np.ix_(lat_idx, lon_idx)]
        v_arr = v_arr[np.ix_(lat_idx, lon_idx)]
        lat_vals = lat_vals[lat_idx]
        lon_vals = lon_vals[lon_idx]
        logger.info("Grille croppée : %dx%d points", u_arr.shape[1], u_arr.shape[0])

    ny, nx = u_arr.shape
    la1 = float(lat_vals[0])   # north
    la2 = float(lat_vals[-1])  # south
    lo1 = float(lon_vals[0])   # west
    lo2 = float(lon_vals[-1])  # east
    dx = round(abs(float(lon_vals[1] - lon_vals[0])), 4)
    dy = round(abs(float(lat_vals[0] - lat_vals[1])), 4)

    valid_time = run_dt + timedelta(hours=fh_hours)
    ref_time = run_dt.strftime("%Y-%m-%dT%H:00:00Z")

    # Replace NaN with 0
    u_flat = np.where(np.isnan(u_arr), 0, u_arr).flatten().tolist()
    v_flat = np.where(np.isnan(v_arr), 0, v_arr).flatten().tolist()

    # leaflet-velocity format
    header_base = {
        "parameterCategory": 2,
        "refTime": ref_time,
        "forecastTime": fh_hours,
        "la1": la1, "lo1": lo1,
        "la2": la2, "lo2": lo2,
        "dx": dx, "dy": dy,
        "nx": nx, "ny": ny,
        "scanMode": 0,
        "gridDefinitionTemplate": 0,
    }

    result = [
        {
            "header": dict(header_base, **{"parameterNumber": 2}),  # U (east)
            "data": [round(v, 3) for v in u_flat],
        },
        {
            "header": dict(header_base, **{"parameterNumber": 3}),  # V (north)
            "data": [round(v, 3) for v in v_flat],
        }
    ]
    return result


def cleanup_old_runs(keep_runs):
    """Supprime les fichiers des runs antérieurs, garde keep_runs runs."""
    index_file = GRIB_CACHE_DIR / "index.json"
    if not index_file.exists():
        return
    try:
        with open(index_file) as f:
            index = json.load(f)
        old_runs = index.get("runs", [])[keep_runs:]
        for run in old_runs:
            run_id = run["run"]
            for fh in FORECAST_HOURS:
                fh_str = f"f{str(fh).zfill(3)}"
                old_file = GRIB_CACHE_DIR / f"wind_{run_id}_{fh_str}.json"
                if old_file.exists():
                    old_file.unlink()
                    logger.info("Supprimé : %s", old_file.name)
    except Exception as e:
        logger.warning("Erreur nettoyage : %s", e)


def main():
    logger.info("=== GRIB Collector démarré ===")
    GRIB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)

    run_dt = find_latest_gfs_run()
    run_id = run_dt.strftime("%Y%m%d_%Hz").replace(":", "").replace("z", "z")
    run_id_clean = run_dt.strftime("%Y%m%d") + "_" + run_dt.strftime("%H") + "z"
    logger.info("Run GFS sélectionné : %s", run_id_clean)

    # Vérifier si ce run est déjà dans le cache
    index_file = GRIB_CACHE_DIR / "index.json"
    existing_index = {}
    if index_file.exists():
        try:
            with open(index_file) as f:
                existing_index = json.load(f)
            existing_runs = [r["run"] for r in existing_index.get("runs", [])]
            if run_id_clean in existing_runs:
                logger.info("Run %s déjà en cache, skip.", run_id_clean)
                cleanup_old_runs(MAX_RUNS)
                return
        except Exception:
            pass

    valid_times = []
    fh_labels = []
    tmp_dir = GRIB_CACHE_DIR / "tmp"
    tmp_dir.mkdir(exist_ok=True)

    success_count = 0

    for fh in FORECAST_HOURS:
        fh_str = f"f{str(fh).zfill(3)}"
        tmp_path = tmp_dir / f"gfs_{run_id_clean}_{fh_str}.grib2"
        out_json = GRIB_CACHE_DIR / f"wind_{run_id_clean}_{fh_str}.json"

        # Download
        ok = download_grib_file(run_dt, fh, tmp_path)
        if not ok:
            logger.warning("Skip fh=%d (téléchargement échoué)", fh)
            if tmp_path.exists():
                tmp_path.unlink()
            continue

        # Parse
        velocity_data = parse_grib_to_velocity_json(tmp_path, run_dt, fh)
        if tmp_path.exists():
            tmp_path.unlink()

        if velocity_data is None:
            logger.warning("Skip fh=%d (parse échoué)", fh)
            continue

        # Save JSON
        try:
            with open(out_json, 'w') as f:
                json.dump(velocity_data, f, separators=(',', ':'))
            size_kb = out_json.stat().st_size // 1024
            logger.info("Sauvegardé %s (%d KB)", out_json.name, size_kb)
        except Exception as e:
            logger.error("Erreur sauvegarde %s : %s", out_json.name, e)
            continue

        valid_time = (run_dt + timedelta(hours=fh)).strftime("%Y-%m-%dT%H:%M") + "Z"
        valid_times.append(valid_time)
        fh_labels.append(fh_str)
        success_count += 1
        time.sleep(1)  # Politesse envers NOMADS

    if success_count == 0:
        logger.error("Aucun fichier téléchargé avec succès")
        return

    # Mettre à jour l'index
    new_run = {
        "run": run_id_clean,
        "run_dt": run_dt.strftime("%Y-%m-%dT%H:00Z"),
        "valid_times": valid_times,
        "fh_labels": fh_labels,
    }

    existing_runs = existing_index.get("runs", [])
    # Insérer en tête, enlever les doublons
    existing_runs = [r for r in existing_runs if r["run"] != run_id_clean]
    existing_runs.insert(0, new_run)

    # Supprimer les vieux runs
    cleanup_old_runs(MAX_RUNS)
    existing_runs = existing_runs[:MAX_RUNS]

    new_index = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "runs": existing_runs,
    }

    with open(index_file, 'w') as f:
        json.dump(new_index, f, indent=2)
    logger.info("Index mis à jour : %d runs, run=%s, %d horizons", len(existing_runs), run_id_clean, success_count)

    # Nettoyage tmp
    try:
        tmp_dir.rmdir()
    except Exception:
        pass

    logger.info("=== GRIB Collector terminé (%d/%d horizons) ===", success_count, len(FORECAST_HOURS))


if __name__ == "__main__":
    main()
