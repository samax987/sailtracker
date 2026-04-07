#!/usr/bin/env python3
"""
inreach_collector.py — Collecteur multi-utilisateur de positions Garmin InReach via MapShare KML.
Tourne en cron toutes les 10 minutes.
"""

import logging
import os
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse, urlencode, urlunparse, parse_qs

import requests

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "SailTracker/1.0 (inreach-collector; contact=samuelvisoko@gmail.com)",
    "Accept": "application/vnd.google-earth.kml+xml, text/xml, */*",
})
from dotenv import load_dotenv

# =============================================================================
# Configuration
# =============================================================================

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = BASE_DIR / "sailtracker.db"
REQUEST_TIMEOUT = 30

# =============================================================================
# Logging
# =============================================================================

log_dir = BASE_DIR / "logs"
log_dir.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("inreach_collector")


# =============================================================================
# Parser KML InReach
# =============================================================================

def parse_kml(kml_text: str) -> list[dict]:
    """Parse le KML MapShare et retourne une liste de positions."""
    positions = []

    # Namespaces KML et GX utilisés par Garmin
    ns = {
        "kml": "http://www.opengis.net/kml/2.2",
        "gx": "http://www.google.com/kml/ext/2.2",
    }

    try:
        root = ET.fromstring(kml_text)
    except ET.ParseError as e:
        logger.error("Erreur parsing KML : %s", e)
        return []

    # Chercher tous les Placemarks dans tous les dossiers
    placemarks = root.findall(".//kml:Placemark", ns)
    if not placemarks:
        # Essai sans namespace
        placemarks = root.findall(".//Placemark")

    for pm in placemarks:
        # Chercher les coordonnées dans <Point><coordinates>
        coords_el = pm.find(".//kml:Point/kml:coordinates", ns)
        if coords_el is None:
            coords_el = pm.find(".//Point/coordinates")
        if coords_el is None or not coords_el.text:
            continue

        coords_text = coords_el.text.strip()
        parts = coords_text.split(",")
        if len(parts) < 2:
            continue

        try:
            lon = float(parts[0])
            lat = float(parts[1])
            alt = float(parts[2]) if len(parts) > 2 else 0.0
        except ValueError:
            continue

        # Timestamp
        ts_el = pm.find(".//kml:TimeStamp/kml:when", ns)
        if ts_el is None:
            ts_el = pm.find(".//TimeStamp/when")

        if ts_el is not None and ts_el.text:
            timestamp_str = ts_el.text.strip()
            # Normaliser en format ISO sans Z
            timestamp_str = timestamp_str.replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(timestamp_str)
                # Convertir en UTC naive pour SQLite
                if dt.tzinfo is not None:
                    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                timestamp = dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                timestamp = timestamp_str[:19]
        else:
            continue

        # Données étendues (vitesse, cap, altitude)
        speed_knots = None
        course = None

        # Chercher dans ExtendedData
        extended = pm.find(".//kml:ExtendedData", ns)
        if extended is None:
            extended = pm.find(".//ExtendedData")

        if extended is not None:
            for data_el in extended.findall(".//kml:Data", ns) or extended.findall(".//Data"):
                name_attr = data_el.get("name", "")
                val_el = data_el.find("kml:value", ns)
                if val_el is None:
                    val_el = data_el.find("value")
                if val_el is None or not val_el.text:
                    continue
                val_text = val_el.text.strip()

                try:
                    if "Velocity" in name_attr or "Speed" in name_attr:
                        # Format possible : "5.2 km/h" ou "2.8 kt"
                        num = float(val_text.split()[0])
                        if "km/h" in val_text or "kph" in val_text.lower():
                            speed_knots = num / 1.852
                        else:
                            speed_knots = num  # Déjà en nœuds ou unité inconnue
                    elif "Course" in name_attr or "Heading" in name_attr:
                        course = float(val_text.split()[0])
                except (ValueError, IndexError):
                    pass

        positions.append({
            "timestamp": timestamp,
            "latitude": lat,
            "longitude": lon,
            "speed_knots": round(speed_knots, 2) if speed_knots is not None else None,
            "course": round(course, 1) if course is not None else None,
            "source": "inreach",
        })

    # Trier par timestamp croissant
    positions.sort(key=lambda p: p["timestamp"])
    return positions


# =============================================================================
# Base de données
# =============================================================================

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def get_last_inreach_timestamp(conn: sqlite3.Connection, user_id: int) -> str | None:
    """Retourne le timestamp de la dernière position InReach pour cet utilisateur."""
    row = conn.execute(
        "SELECT MAX(timestamp) FROM positions WHERE source = 'inreach' AND user_id = ?",
        (user_id,)
    ).fetchone()
    return row[0] if row else None


def insert_positions(conn: sqlite3.Connection, positions: list[dict], user_id: int) -> int:
    """Insère les positions dans la table positions avec user_id. Retourne le nombre d'insertions."""
    inserted = 0
    for pos in positions:
        try:
            conn.execute(
                """
                INSERT INTO positions
                    (timestamp, latitude, longitude, speed_knots, course, heading, nav_status, source, user_id)
                VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    pos["timestamp"],
                    pos["latitude"],
                    pos["longitude"],
                    pos["speed_knots"],
                    pos["course"],
                    pos["source"],
                    user_id,
                ),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            pass  # Doublon sur timestamp + source (si contrainte existe)
    conn.commit()
    return inserted


# =============================================================================
# Collecte pour un utilisateur
# =============================================================================

def collect_for_user(user_id: int, share_url: str, feed_password: str | None,
                     username: str = "?", boat_name: str = "?"):
    """Collecte les positions InReach pour un utilisateur donné."""
    logger.info("=== Collecte pour %s (%s) user_id=%d ===", boat_name, username, user_id)

    conn = get_db()
    last_ts = get_last_inreach_timestamp(conn, user_id)
    logger.info("Dernière position en base : %s", last_ts or "aucune")

    # Construire l'URL avec les paramètres de date
    if last_ts:
        try:
            d1_dt = datetime.fromisoformat(last_ts) - timedelta(hours=1)
        except Exception:
            d1_dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
    else:
        d1_dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)

    d1_str = d1_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    d2_str = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")

    parsed = urlparse(share_url)
    params = parse_qs(parsed.query)
    params["d1"] = [d1_str]
    params["d2"] = [d2_str]
    if feed_password:
        params["password"] = [feed_password]
    kml_url = urlunparse(parsed._replace(query=urlencode(params, doseq=True)))

    logger.info("Collecte InReach depuis : %s (d1=%s)", share_url, d1_str)

    # Télécharger le KML
    try:
        resp = _SESSION.get(kml_url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        kml_text = resp.text
    except requests.RequestException as e:
        logger.error("Erreur HTTP lors de la collecte KML user_id=%d : %s", user_id, e)
        conn.close()
        return

    logger.info("KML téléchargé (%d octets)", len(kml_text))

    # Parser le KML
    positions = parse_kml(kml_text)
    logger.info("%d positions trouvées dans le KML", len(positions))

    if not positions:
        logger.info("Aucune position à traiter.")
        # Mettre à jour last_fetched
        conn.execute(
            "UPDATE inreach_configs SET last_fetched=datetime('now') WHERE user_id=?",
            (user_id,)
        )
        conn.commit()
        conn.close()
        return

    # Filtrer les doublons
    if last_ts:
        positions = [p for p in positions if p["timestamp"] > last_ts]

    logger.info("%d nouvelles positions à insérer", len(positions))

    if positions:
        inserted = insert_positions(conn, positions, user_id)
        logger.info("%d positions insérées pour user_id=%d", inserted, user_id)
        last = positions[-1]
        logger.info(
            "Dernière position : %s — lat=%.6f lon=%.6f spd=%s cap=%s",
            last["timestamp"],
            last["latitude"],
            last["longitude"],
            f"{last['speed_knots']:.1f} kn" if last["speed_knots"] is not None else "—",
            f"{last['course']:.0f}°" if last["course"] is not None else "—",
        )
    else:
        logger.info("Aucune nouvelle position à insérer pour user_id=%d.", user_id)

    # Mettre à jour last_fetched
    conn.execute(
        "UPDATE inreach_configs SET last_fetched=datetime('now') WHERE user_id=?",
        (user_id,)
    )
    conn.commit()
    conn.close()


# =============================================================================
# Main
# =============================================================================

def main():
    conn = get_db()
    try:
        configs = conn.execute(
            "SELECT ic.id, ic.user_id, ic.share_url, ic.feed_password, u.username, u.boat_name "
            "FROM inreach_configs ic JOIN users u ON ic.user_id=u.id WHERE ic.enabled=1"
        ).fetchall()
    except Exception as e:
        logger.error("Erreur lecture inreach_configs (table peut-être absente) : %s", e)
        configs = []
    conn.close()

    if not configs:
        # Rétro-compatibilité : lire l'URL depuis .env pour user_id=1
        url = os.getenv("INREACH_KML_URL", "")
        if url:
            logger.info("Pas de config DB, utilisation de INREACH_KML_URL (user_id=1)")
            collect_for_user(user_id=1, share_url=url, feed_password=None,
                             username="sam", boat_name="POLLEN 1")
        else:
            logger.error("INREACH_KML_URL non configuré et aucune config DB — arrêt.")
        return

    for cfg in configs:
        collect_for_user(
            user_id=cfg['user_id'],
            share_url=cfg['share_url'],
            feed_password=cfg['feed_password'],
            username=cfg['username'],
            boat_name=cfg['boat_name'],
        )


if __name__ == "__main__":
    main()
