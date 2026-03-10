#!/usr/bin/env python3
"""
inreach_collector.py — Collecteur de positions Garmin InReach via MapShare KML.
Tourne en cron toutes les 10 minutes.
"""

import logging
import os
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# =============================================================================
# Configuration
# =============================================================================

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = BASE_DIR / "sailtracker.db"
INREACH_KML_URL = os.getenv("INREACH_KML_URL", "")
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

def get_last_inreach_timestamp(conn: sqlite3.Connection) -> str | None:
    """Retourne le timestamp de la dernière position InReach en base."""
    row = conn.execute(
        "SELECT MAX(timestamp) FROM positions WHERE source = 'inreach'"
    ).fetchone()
    return row[0] if row else None


def insert_positions(conn: sqlite3.Connection, positions: list[dict]) -> int:
    """Insère les positions dans la table positions. Retourne le nombre d'insertions."""
    inserted = 0
    for pos in positions:
        try:
            conn.execute(
                """
                INSERT INTO positions
                    (timestamp, latitude, longitude, speed_knots, course, heading, nav_status, source)
                VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    pos["timestamp"],
                    pos["latitude"],
                    pos["longitude"],
                    pos["speed_knots"],
                    pos["course"],
                    pos["source"],
                ),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            pass  # Doublon sur timestamp + source (si contrainte existe)
    conn.commit()
    return inserted


# =============================================================================
# Main
# =============================================================================

def main():
    if not INREACH_KML_URL:
        logger.error("INREACH_KML_URL non configuré dans .env — arrêt.")
        return

    logger.info("Collecte InReach depuis : %s", INREACH_KML_URL)

    # Télécharger le KML
    try:
        resp = requests.get(INREACH_KML_URL, timeout=REQUEST_TIMEOUT, headers={
            "User-Agent": "SailTracker/1.0",
            "Accept": "application/vnd.google-earth.kml+xml, text/xml, */*",
        })
        resp.raise_for_status()
        kml_text = resp.text
    except requests.RequestException as e:
        logger.error("Erreur HTTP lors de la collecte KML : %s", e)
        return

    logger.info("KML téléchargé (%d octets)", len(kml_text))

    # Parser le KML
    positions = parse_kml(kml_text)
    logger.info("%d positions trouvées dans le KML", len(positions))

    if not positions:
        logger.info("Aucune position à traiter.")
        return

    # Connexion DB
    conn = sqlite3.connect(DB_PATH, timeout=10)

    # Filtrer les doublons par rapport à la dernière position en base
    last_ts = get_last_inreach_timestamp(conn)
    logger.info("Dernière position InReach en base : %s", last_ts or "aucune")

    if last_ts:
        positions = [p for p in positions if p["timestamp"] > last_ts]

    logger.info("%d nouvelles positions à insérer", len(positions))

    if positions:
        inserted = insert_positions(conn, positions)
        logger.info("%d positions insérées", inserted)
        # Afficher la dernière position
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
        logger.info("Aucune nouvelle position à insérer.")

    conn.close()


if __name__ == "__main__":
    main()
