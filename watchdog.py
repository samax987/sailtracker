#!/usr/bin/env python3
"""
watchdog.py — Surveillance SailTracker toutes les 30 min.
Vérifie : Flask, passage_planner, weather_collector, SQLite, disque, RAM.
Alerte Telegram si problème (anti-spam : max 1 alerte/problème/6h).
Nettoyage auto DB : ensemble_forecasts > 7j, passage_forecasts > 14j, VACUUM si > 100 MB.
"""

import json
import logging
import math
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = BASE_DIR / "sailtracker.db"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SERVER_URL = os.getenv("SERVER_URL", "http://localhost:8085")
ALERT_STATE_FILE = Path("/tmp/watchdog_last_alert.json")
ALERT_COOLDOWN_H = 6  # heures entre deux alertes du même type

# Seuils
FLASK_TIMEOUT_S = 10
MAX_PASSAGE_PLANNER_AGE_H = 12
MAX_WEATHER_AGE_H = 6
MIN_DISK_FREE_GB = 1.0
MAX_RAM_PCT = 90.0
DB_VACUUM_THRESHOLD_MB = 100
ENSEMBLE_RETENTION_DAYS = 7
PASSAGE_FORECASTS_RETENTION_DAYS = 14

import logging.handlers

log_dir = BASE_DIR / "logs"
log_dir.mkdir(exist_ok=True)

logger = logging.getLogger("watchdog")
logger.setLevel(logging.INFO)
_rot = logging.handlers.RotatingFileHandler(
    log_dir / "watchdog.log", maxBytes=5*1024*1024, backupCount=3, encoding="utf-8"
)
_con = logging.StreamHandler()
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_rot.setFormatter(_fmt)
_con.setFormatter(_fmt)
logger.addHandler(_rot)
logger.addHandler(_con)
logger.propagate = False


# =============================================================================
# Telegram
# =============================================================================

def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error("Erreur Telegram : %s", e)
        return False


# =============================================================================
# Anti-spam
# =============================================================================

def load_alert_state() -> dict:
    try:
        if ALERT_STATE_FILE.exists():
            return json.loads(ALERT_STATE_FILE.read_text())
    except Exception:
        pass
    return {}


def save_alert_state(state: dict) -> None:
    try:
        ALERT_STATE_FILE.write_text(json.dumps(state))
    except Exception as e:
        logger.warning("Impossible de sauvegarder l'état des alertes : %s", e)


def should_send_alert(state: dict, key: str) -> bool:
    """Retourne True si l'alerte doit être envoyée (pas envoyée dans les 6h)."""
    last = state.get(key)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
        return age_h >= ALERT_COOLDOWN_H
    except Exception:
        return True


def mark_alert_sent(state: dict, key: str) -> None:
    state[key] = datetime.now(timezone.utc).isoformat()


# =============================================================================
# Checks
# =============================================================================

def check_flask() -> tuple[bool, str]:
    """Vérifie que le serveur Flask répond à /api/health."""
    try:
        resp = requests.get("http://127.0.0.1:8085/api/health", timeout=FLASK_TIMEOUT_S)
        if resp.status_code == 200:
            return True, "OK"
        return False, f"HTTP {resp.status_code}"
    except requests.ConnectionError:
        return False, "Connexion refusée (Flask arrêté ?)"
    except requests.Timeout:
        return False, f"Timeout ({FLASK_TIMEOUT_S}s)"
    except Exception as e:
        return False, str(e)


def check_passage_planner(conn) -> tuple[bool, str]:
    """Vérifie que passage_planner a tourné dans les 12 dernières heures."""
    row = conn.execute(
        "SELECT MAX(computed_at) as last FROM departure_simulations"
    ).fetchone()
    if not row or not row["last"]:
        return False, "Aucune simulation trouvée en base"
    try:
        last = datetime.fromisoformat(row["last"].replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        if age_h > MAX_PASSAGE_PLANNER_AGE_H:
            return False, f"Dernière simulation il y a {age_h:.1f}h (max {MAX_PASSAGE_PLANNER_AGE_H}h)"
        return True, f"OK ({age_h:.1f}h)"
    except Exception as e:
        return False, f"Erreur parsing timestamp : {e}"


def check_weather_collector(conn) -> tuple[bool, str]:
    """Vérifie que weather_collector a tourné dans les 6 dernières heures."""
    row = conn.execute(
        "SELECT MAX(collected_at) as last FROM weather_snapshots"
    ).fetchone()
    if not row or not row["last"]:
        return False, "Aucun snapshot météo en base"
    try:
        last = datetime.fromisoformat(row["last"].replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        if age_h > MAX_WEATHER_AGE_H:
            return False, f"Dernier snapshot il y a {age_h:.1f}h (max {MAX_WEATHER_AGE_H}h)"
        return True, f"OK ({age_h:.1f}h)"
    except Exception as e:
        return False, f"Erreur parsing timestamp : {e}"


def check_sqlite_integrity() -> tuple[bool, str]:
    """Vérifie l'intégrité de la base SQLite (PRAGMA quick_check)."""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        result = conn.execute("PRAGMA quick_check").fetchone()
        conn.close()
        if result and result[0] == "ok":
            return True, "OK"
        return False, f"PRAGMA quick_check : {result[0] if result else 'aucun résultat'}"
    except Exception as e:
        return False, f"Impossible d'ouvrir la DB : {e}"


def check_disk_space() -> tuple[bool, str]:
    """Vérifie qu'il reste au moins 1 GB libre sur la partition principale."""
    try:
        usage = shutil.disk_usage("/var/www/sailtracker")
        free_gb = usage.free / (1024**3)
        total_gb = usage.total / (1024**3)
        if free_gb < MIN_DISK_FREE_GB:
            return False, f"Espace disque critique : {free_gb:.1f} GB libres (min {MIN_DISK_FREE_GB} GB)"
        return True, f"OK ({free_gb:.1f} GB libres / {total_gb:.0f} GB)"
    except Exception as e:
        return False, str(e)


def check_ram() -> tuple[bool, str]:
    """Vérifie que la RAM utilisée est < 90%."""
    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
        mem = {}
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                mem[parts[0].rstrip(":")] = int(parts[1])
        total = mem.get("MemTotal", 0)
        available = mem.get("MemAvailable", 0)
        if total == 0:
            return False, "Impossible de lire /proc/meminfo"
        used_pct = (total - available) / total * 100
        if used_pct > MAX_RAM_PCT:
            used_mb = (total - available) // 1024
            total_mb = total // 1024
            return False, f"RAM critique : {used_pct:.0f}% ({used_mb} MB / {total_mb} MB)"
        return True, f"OK ({used_pct:.0f}%)"
    except Exception as e:
        return False, str(e)


# =============================================================================
# Maintenance DB
# =============================================================================

def run_db_maintenance(conn) -> list[str]:
    """Nettoie les anciennes données et fait un VACUUM si nécessaire."""
    actions = []

    # Supprimer ensemble_forecasts > 7 jours
    cur = conn.execute(
        "DELETE FROM ensemble_forecasts WHERE collected_at < datetime('now', ?)",
        (f"-{ENSEMBLE_RETENTION_DAYS} days",),
    )
    if cur.rowcount > 0:
        conn.commit()
        actions.append(f"ensemble_forecasts : {cur.rowcount} lignes supprimées (>{ENSEMBLE_RETENTION_DAYS}j)")
        logger.info("DB maintenance : ensemble_forecasts — %d lignes supprimées", cur.rowcount)

    # Supprimer passage_forecasts > 14 jours
    cur = conn.execute(
        "DELETE FROM passage_forecasts WHERE collected_at < datetime('now', ?)",
        (f"-{PASSAGE_FORECASTS_RETENTION_DAYS} days",),
    )
    if cur.rowcount > 0:
        conn.commit()
        actions.append(f"passage_forecasts : {cur.rowcount} lignes supprimées (>{PASSAGE_FORECASTS_RETENTION_DAYS}j)")
        logger.info("DB maintenance : passage_forecasts — %d lignes supprimées", cur.rowcount)

    # Supprimer departure_simulations > 30 jours
    cur = conn.execute("DELETE FROM departure_simulations WHERE computed_at < datetime('now', '-30 days')")
    if cur.rowcount > 0:
        conn.commit()
        actions.append(f"departure_simulations : {cur.rowcount} lignes supprimées (>30j)")
        logger.info("DB maintenance : departure_simulations — %d lignes supprimées", cur.rowcount)

    conn.close()

    # VACUUM si DB > 100 MB
    db_size_mb = DB_PATH.stat().st_size / (1024**2)
    if db_size_mb > DB_VACUUM_THRESHOLD_MB:
        logger.info("DB maintenance : VACUUM (taille actuelle %.1f MB)...", db_size_mb)
        vconn = sqlite3.connect(str(DB_PATH), timeout=120)
        vconn.execute("VACUUM")
        vconn.close()
        new_size_mb = DB_PATH.stat().st_size / (1024**2)
        saved_mb = db_size_mb - new_size_mb
        actions.append(f"VACUUM effectué : {db_size_mb:.0f} MB → {new_size_mb:.0f} MB (−{saved_mb:.0f} MB)")
        logger.info("DB maintenance : VACUUM OK — %.1f MB → %.1f MB", db_size_mb, new_size_mb)
    else:
        logger.info("DB maintenance : pas de VACUUM nécessaire (%.1f MB < %d MB)", db_size_mb, DB_VACUUM_THRESHOLD_MB)

    return actions


# =============================================================================
# Main
# =============================================================================

def main():
    logger.info("=== Watchdog SailTracker démarré ===")
    now_str = datetime.now(timezone.utc).strftime("%d/%m %H:%M UTC")

    alert_state = load_alert_state()
    failures = []  # liste de (key, message)

    # ── 1. Flask ──────────────────────────────────────────────────────────────
    ok, detail = check_flask()
    if ok:
        logger.info("Flask : %s", detail)
    else:
        logger.warning("Flask FAIL : %s", detail)
        failures.append(("flask", f"🔴 <b>Serveur Flask hors ligne</b>\n{detail}"))

    # ── 2. Passage planner ────────────────────────────────────────────────────
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    ok, detail = check_passage_planner(conn)
    if ok:
        logger.info("Passage planner : %s", detail)
    else:
        logger.warning("Passage planner FAIL : %s", detail)
        failures.append(("passage_planner", f"🟠 <b>Passage planner inactif</b>\n{detail}"))

    # ── 3. Weather collector ──────────────────────────────────────────────────
    ok, detail = check_weather_collector(conn)
    if ok:
        logger.info("Weather collector : %s", detail)
    else:
        logger.warning("Weather collector FAIL : %s", detail)
        failures.append(("weather", f"🟠 <b>Weather collector inactif</b>\n{detail}"))

    # ── 4. Maintenance DB (toujours, pas seulement en cas d'erreur) ───────────
    maintenance_actions = run_db_maintenance(conn)  # conn est fermé dans la fonction

    # ── 5. Intégrité SQLite ───────────────────────────────────────────────────
    ok, detail = check_sqlite_integrity()
    if ok:
        logger.info("SQLite integrity : %s", detail)
    else:
        logger.error("SQLite FAIL : %s", detail)
        failures.append(("sqlite", f"🔴 <b>Base de données corrompue !</b>\n{detail}"))

    # ── 6. Espace disque ──────────────────────────────────────────────────────
    ok, detail = check_disk_space()
    if ok:
        logger.info("Disque : %s", detail)
    else:
        logger.warning("Disque FAIL : %s", detail)
        failures.append(("disk", f"🔴 <b>Espace disque critique</b>\n{detail}"))

    # ── 7. RAM ────────────────────────────────────────────────────────────────
    ok, detail = check_ram()
    if ok:
        logger.info("RAM : %s", detail)
    else:
        logger.warning("RAM FAIL : %s", detail)
        failures.append(("ram", f"🟠 <b>RAM saturée</b>\n{detail}"))

    # ── Envoi alertes Telegram (anti-spam) ────────────────────────────────────
    alerts_sent = 0
    for key, msg in failures:
        if should_send_alert(alert_state, key):
            full_msg = (
                f"⚠️ <b>WATCHDOG SailTracker — {now_str}</b>\n\n"
                f"{msg}\n\n"
                f"<a href='{SERVER_URL}/passage'>📊 Tableau de bord</a>"
            )
            if send_telegram(full_msg):
                mark_alert_sent(alert_state, key)
                alerts_sent += 1
                logger.info("Alerte Telegram envoyée : %s", key)
        else:
            logger.info("Alerte %s supprimée (cooldown 6h)", key)

    # Effacer les états des checks qui sont repassés OK
    ok_keys = {"flask", "passage_planner", "weather", "sqlite", "disk", "ram"}
    failing_keys = {k for k, _ in failures}
    for key in ok_keys - failing_keys:
        alert_state.pop(key, None)  # reset le cooldown quand le problème est résolu

    save_alert_state(alert_state)

    # ── Résumé maintenance ────────────────────────────────────────────────────
    if maintenance_actions:
        logger.info("Maintenance DB : %s", " | ".join(maintenance_actions))

    if not failures:
        logger.info("=== Tous les checks OK ===")
    else:
        logger.warning("=== %d check(s) en échec, %d alerte(s) envoyée(s) ===", len(failures), alerts_sent)


if __name__ == "__main__":
    main()
