"""
rust_engine.py — Wrapper Python pour le moteur de calcul Rust SailTracker

Appelle le binaire Rust via subprocess et fournit un fallback Python
transparent si le binaire est absent ou en erreur.
"""

import json
import logging
import logging.handlers
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

_BASE = Path("/var/www/sailtracker")
ENGINE_PATH = _BASE / "engine" / "target" / "release" / "sailtracker-engine"
POLARS_PATH = str(_BASE / "sailtracker.db")

# Timeout par commande (secondes)
TIMEOUTS = {
    "polar": 5,
    "route": 30,
    "optimize": 300,
    "score": 120,
    "ensemble": 10,
    "version": 5,
}

# ─── Logger dédié moteur ──────────────────────────────────────────────────────

def _setup_engine_logger() -> logging.Logger:
    log = logging.getLogger("sailtracker_engine")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    log_dir = _BASE / "logs"
    log_dir.mkdir(exist_ok=True)
    h = logging.handlers.RotatingFileHandler(
        log_dir / "engine.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fmt = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    h.setFormatter(fmt)
    log.addHandler(h)
    return log

_elog = _setup_engine_logger()
logger = logging.getLogger("sailtracker_server")

# ─── État partagé (last call stats) ──────────────────────────────────────────

_state = {
    "last_rust_call": None,        # ISO timestamp
    "last_rust_duration_ms": None, # float
    "last_python_fallback": None,  # ISO timestamp
    "last_python_command": None,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record_rust_success(command: str, duration_ms: float):
    _state["last_rust_call"] = _now_iso()
    _state["last_rust_duration_ms"] = round(duration_ms, 1)
    _elog.info("ENGINE: Rust called for %s — completed in %.0fms", command, duration_ms)


def _record_python_fallback(command: str, duration_ms: float):
    _state["last_python_fallback"] = _now_iso()
    _state["last_python_command"] = command
    _elog.info("ENGINE: Rust unavailable, Python fallback for %s — completed in %.0fms", command, duration_ms)


# ─── Appel bas niveau ──────────────────────────────────────────────────────────

def _call_engine(command: str, input_data: dict, polars: bool = True) -> dict:
    """
    Appelle le moteur Rust et retourne le résultat JSON.
    Lève RuntimeError si le binaire échoue.
    """
    if not ENGINE_PATH.exists():
        raise FileNotFoundError(f"Binaire Rust introuvable : {ENGINE_PATH}")

    cmd = [str(ENGINE_PATH), command]
    if polars:
        cmd += ["--polars", POLARS_PATH]

    t0 = time.monotonic()
    result = subprocess.run(
        cmd,
        input=json.dumps(input_data),
        capture_output=True,
        text=True,
        timeout=TIMEOUTS.get(command, 60),
    )
    elapsed_ms = (time.monotonic() - t0) * 1000

    if result.returncode != 0:
        err = result.stderr.strip() or "erreur inconnue"
        raise RuntimeError(f"Rust engine '{command}' failed: {err}")

    _record_rust_success(command, elapsed_ms)
    return json.loads(result.stdout)


def _call_engine_no_stdin(command: str) -> dict:
    """Appel sans stdin (pour 'version')."""
    if not ENGINE_PATH.exists():
        raise FileNotFoundError(f"Binaire Rust introuvable : {ENGINE_PATH}")
    t0 = time.monotonic()
    result = subprocess.run(
        [str(ENGINE_PATH), command],
        capture_output=True, text=True,
        timeout=TIMEOUTS.get(command, 5),
    )
    elapsed_ms = (time.monotonic() - t0) * 1000
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "erreur inconnue")
    _record_rust_success(command, elapsed_ms)
    return json.loads(result.stdout)


# ─── API publique ─────────────────────────────────────────────────────────────

def rust_polar(twa: float, tws: float) -> dict | None:
    """
    Calcule la vitesse bateau pour un TWA/TWS.
    Retourne {"speed_kts": float, "vmg_kts": float} ou None si indisponible.
    """
    t0 = time.monotonic()
    try:
        return _call_engine("polar", {"twa": twa, "tws": tws})
    except Exception as e:
        _record_python_fallback("polar", (time.monotonic() - t0) * 1000)
        logger.debug("rust_polar unavailable: %s", e)
        return None


def rust_route(waypoints: list, forecasts: list, departure_time: str = None,
               boat_speed_fixed_kts: float = 6.0, limits: dict = None) -> dict | None:
    """
    Calcule l'ETA d'une route avec polaires, vent et courants.
    Retourne le résultat JSON ou None si indisponible.
    """
    payload = {
        "waypoints": waypoints,
        "forecasts": forecasts,
        "boat_speed_fixed_kts": boat_speed_fixed_kts,
    }
    if departure_time:
        payload["departure_time"] = departure_time
    if limits:
        payload["limits"] = limits
    t0 = time.monotonic()
    try:
        return _call_engine("route", payload)
    except Exception as e:
        _record_python_fallback("route", (time.monotonic() - t0) * 1000)
        logger.debug("rust_route unavailable: %s", e)
        return None


def rust_optimize(start: dict, end: dict, departure_time: str,
                  wind_grid: list, limits: dict = None,
                  boat_speed_fixed_kts: float = 6.0,
                  max_deviation_nm: float = 300.0,
                  angle_step_deg: float = 5.0) -> dict | None:
    """
    Lance le routage par isochrones Rust.
    Retourne le résultat JSON ou None si indisponible.
    """
    payload = {
        "start": start,
        "end": end,
        "departure_time": departure_time,
        "wind_grid": wind_grid,
        "max_deviation_nm": max_deviation_nm,
        "angle_step_deg": angle_step_deg,
        "time_step_hours": 1.0,
        "boat_speed_fixed_kts": boat_speed_fixed_kts,
    }
    if limits:
        payload["limits"] = limits
    t0 = time.monotonic()
    try:
        return _call_engine("optimize", payload)
    except Exception as e:
        _record_python_fallback("optimize", (time.monotonic() - t0) * 1000)
        logger.warning("rust_optimize unavailable: %s", e)
        return None


def rust_score(waypoints: list, departure_datetimes: list,
               forecasts_by_wp: dict, limits: dict = None,
               boat_speed_avg_kts: float = 6.0) -> list | None:
    """
    Calcule les scores de départ pour N dates en parallèle.

    forecasts_by_wp : {"0": [{"forecast_time": ..., "conditions": {...}}, ...], "1": [...]}

    Retourne la liste des DepartureResult ou None si le moteur est indisponible.
    """
    payload = {
        "waypoints": waypoints,
        "departure_datetimes": departure_datetimes,
        "forecasts": forecasts_by_wp,
        "boat_speed_avg_kts": boat_speed_avg_kts,
    }
    if limits:
        payload["limits"] = limits
    t0 = time.monotonic()
    try:
        result = _call_engine("score", payload)
        return result.get("departures", [])
    except Exception as e:
        _record_python_fallback("score", (time.monotonic() - t0) * 1000)
        logger.warning("rust_score unavailable, falling back to Python: %s", e)
        return None


def rust_ensemble(members: list[float]) -> dict | None:
    """
    Calcule les statistiques d'ensemble (mean, std, percentiles).
    Retourne le résultat JSON ou None si indisponible.
    """
    t0 = time.monotonic()
    try:
        return _call_engine("ensemble", {"members": members}, polars=False)
    except Exception as e:
        _record_python_fallback("ensemble", (time.monotonic() - t0) * 1000)
        logger.debug("rust_ensemble unavailable: %s", e)
        return None


def rust_version() -> dict | None:
    """Retourne les infos de version du binaire Rust, ou None si indisponible."""
    try:
        return _call_engine_no_stdin("version")
    except Exception as e:
        logger.debug("rust_version unavailable: %s", e)
        return None


def engine_available() -> bool:
    """Vérifie si le binaire Rust est disponible et fonctionnel."""
    try:
        result = _call_engine("ensemble", {"members": [1.0, 2.0, 3.0]}, polars=False)
        return "mean" in result
    except Exception:
        return False


def engine_state() -> dict:
    """Retourne l'état courant du moteur (timestamps, durées, disponibilité binaire)."""
    return {
        "rust_binary_exists": ENGINE_PATH.exists(),
        "rust_binary_path": str(ENGINE_PATH),
        **_state,
    }
