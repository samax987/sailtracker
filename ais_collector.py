#!/usr/bin/env python3
"""
ais_collector.py — Collecteur de positions AIS en temps réel
Connexion permanente au websocket aisstream.io, stockage dans SQLite.
Tourne comme daemon systemd (sailtracker-ais.service).
"""

import asyncio
import json
import logging
import logging.handlers
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import ssl
import aiohttp
import certifi
import websockets
from dotenv import load_dotenv

# =============================================================================
# Configuration
# =============================================================================

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

API_KEY = os.getenv("AISSTREAM_API_KEY", "")
VESSEL_MMSI = int(os.getenv("VESSEL_MMSI", "0"))
AISHUB_USERNAME = os.getenv("AISHUB_USERNAME", "")
DB_PATH = BASE_DIR / "sailtracker.db"
AIS_WS_URL = "wss://stream.aisstream.io/v0/stream"
AISHUB_API_URL = "https://data.aishub.net/ws.php"

# Reconnexion websocket aisstream.io
RECONNECT_INITIAL_DELAY = 5   # secondes
RECONNECT_MAX_DELAY = 300     # 5 minutes max
RECONNECT_BACKOFF = 2.0

# Polling AISHub : intervalle entre chaque requête HTTP
AISHUB_POLL_INTERVAL = 60  # secondes

# Mapping NavigationalStatus (entier AIS → texte lisible)
NAV_STATUS_MAP = {
    0:  "Under way using engine",
    1:  "At anchor",
    2:  "Not under command",
    3:  "Restricted manoeuvrability",
    4:  "Constrained by draught",
    5:  "Moored",
    6:  "Aground",
    7:  "Engaged in fishing",
    8:  "Under way sailing",
    9:  "Reserved",
    10: "Reserved",
    11: "Power-driven vessel towing astern",
    12: "Power-driven vessel pushing ahead",
    13: "Reserved",
    14: "AIS-SART",
    15: "Not defined",
}

# =============================================================================
# Logging avec rotation
# =============================================================================

def setup_logging() -> logging.Logger:
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)

    logger = logging.getLogger("ais_collector")
    logger.setLevel(logging.INFO)
    logger.propagate = False  # Évite le double logging via le root logger

    # Rotation : max 10 MB, 5 fichiers de backup
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "ais.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # Aussi sur stdout pour systemd journal
    console.setFormatter(formatter)
    return logger


logger = setup_logging()

# =============================================================================
# Base de données
# =============================================================================

def init_db() -> None:
    """Crée les tables si elles n'existent pas encore."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            speed_knots REAL,
            course REAL,
            heading REAL,
            nav_status TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_positions_timestamp
            ON positions(timestamp DESC);
    """)
    conn.commit()
    conn.close()


def save_position(
    timestamp: str,
    lat: float,
    lon: float,
    speed: float | None,
    course: float | None,
    heading: float | None,
    nav_status: str | None,
) -> None:
    """Insère une position et purge les données de plus de 30 jours."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()

    c.execute(
        """
        INSERT INTO positions (timestamp, latitude, longitude,
            speed_knots, course, heading, nav_status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (timestamp, lat, lon, speed, course, heading, nav_status),
    )

    # Nettoyage : conserver max 30 jours d'historique
    c.execute(
        """
        DELETE FROM positions
        WHERE created_at < datetime('now', '-30 days')
        """
    )

    conn.commit()
    conn.close()


# =============================================================================
# Traitement des messages AIS
# =============================================================================

def parse_nav_status(status_code: int | None) -> str:
    """Convertit le code entier NavigationalStatus en texte."""
    if status_code is None:
        return "Not defined"
    return NAV_STATUS_MAP.get(int(status_code), f"Unknown ({status_code})")


def parse_position_report(message: dict) -> dict | None:
    """
    Extrait les champs utiles d'un message PositionReport aisstream.io.
    Retourne None si le message n'est pas valide.
    """
    try:
        msg_type = message.get("MessageType", "")
        if msg_type != "PositionReport":
            return None

        report = message["Message"]["PositionReport"]
        metadata = message.get("MetaData", {})

        # Timestamp : depuis MetaData si disponible, sinon UTC now
        raw_time = metadata.get("time_utc", "")
        if raw_time:
            # Format : "2025-02-22 14:30:00.000000 +0000 UTC"
            try:
                ts = datetime.strptime(
                    raw_time[:26], "%Y-%m-%d %H:%M:%S.%f"
                ).replace(tzinfo=timezone.utc)
                timestamp = ts.isoformat()
            except ValueError:
                timestamp = datetime.now(timezone.utc).isoformat()
        else:
            timestamp = datetime.now(timezone.utc).isoformat()

        lat = float(report.get("Latitude", 0))
        lon = float(report.get("Longitude", 0))

        # Positions invalides (0,0 ou hors limites)
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            return None
        if lat == 0.0 and lon == 0.0:
            return None

        sog = report.get("Sog")  # Speed Over Ground (nœuds)
        cog = report.get("Cog")  # Course Over Ground (degrés)

        heading = report.get("TrueHeading")
        # 511 = valeur "non disponible" dans le protocole AIS
        if heading is not None and int(heading) == 511:
            heading = None

        nav_code = report.get("NavigationalStatus")
        nav_status = parse_nav_status(nav_code)

        return {
            "timestamp": timestamp,
            "latitude": lat,
            "longitude": lon,
            "speed_knots": float(sog) if sog is not None else None,
            "course": float(cog) if cog is not None else None,
            "heading": float(heading) if heading is not None else None,
            "nav_status": nav_status,
            "ship_name": metadata.get("ShipName", ""),
        }

    except (KeyError, TypeError, ValueError) as e:
        logger.debug("Erreur parsing PositionReport : %s", e)
        return None


# =============================================================================
# Client WebSocket
# =============================================================================

async def subscribe_message(mmsi: int) -> str:
    """Construit le message de souscription aisstream.io."""
    return json.dumps({
        "APIKey": API_KEY,
        "BoundingBoxes": [[[-90, -180], [90, 180]]],  # Monde entier
        "FiltersShipMMSI": [str(mmsi)],
        "FilterMessageTypes": ["PositionReport"],
    })


async def run_ais_collector() -> None:
    """
    Boucle principale : connexion WebSocket avec reconnexion automatique
    en cas de déconnexion ou d'erreur réseau.
    """
    if not API_KEY or API_KEY == "REMPLACE_PAR_TA_CLE_AISSTREAM":
        logger.error("AISSTREAM_API_KEY non configurée dans .env. Arrêt.")
        return

    if not VESSEL_MMSI or VESSEL_MMSI == 0:
        logger.error("VESSEL_MMSI non configuré dans .env. Arrêt.")
        return

    delay = RECONNECT_INITIAL_DELAY
    attempt = 0

    while True:
        attempt += 1
        logger.info(
            "Tentative de connexion #%d à %s (MMSI: %d)",
            attempt, AIS_WS_URL, VESSEL_MMSI
        )

        try:
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            async with websockets.connect(
                AIS_WS_URL,
                ssl=ssl_ctx,
                open_timeout=30,
                ping_interval=30,
                ping_timeout=15,
                close_timeout=10,
            ) as ws:
                logger.info("Connecté à aisstream.io")
                delay = RECONNECT_INITIAL_DELAY  # Reset après connexion réussie

                # Envoi de la souscription
                sub_msg = await subscribe_message(VESSEL_MMSI)
                await ws.send(sub_msg)
                logger.info("Souscription envoyée pour MMSI %d", VESSEL_MMSI)

                async for raw_message in ws:
                    try:
                        data = json.loads(raw_message)
                    except json.JSONDecodeError:
                        logger.warning("Message non-JSON reçu, ignoré")
                        continue

                    position = parse_position_report(data)
                    if position is None:
                        # Message d'un autre type ou invalide
                        msg_type = data.get("MessageType", "unknown")
                        if msg_type != "PositionReport":
                            logger.debug("Message ignoré (type: %s)", msg_type)
                        continue

                    save_position(
                        position["timestamp"],
                        position["latitude"],
                        position["longitude"],
                        position["speed_knots"],
                        position["course"],
                        position["heading"],
                        position["nav_status"],
                    )

                    logger.info(
                        "[%s] %s — %.4f°N %.4f°E — %.1f kn — Cap %.0f° — %s",
                        position["timestamp"][:19],
                        position["ship_name"] or f"MMSI:{VESSEL_MMSI}",
                        position["latitude"],
                        position["longitude"],
                        position["speed_knots"] or 0.0,
                        position["course"] or 0.0,
                        position["nav_status"],
                    )

        except websockets.exceptions.ConnectionClosedError as e:
            logger.warning("Connexion fermée par le serveur : %s", e)
        except websockets.exceptions.WebSocketException as e:
            logger.error("Erreur WebSocket : %s", e)
        except OSError as e:
            logger.error("Erreur réseau : %s", e)
        except Exception as e:  # noqa: BLE001
            logger.exception("Erreur inattendue : %s", e)

        logger.info("Reconnexion dans %ds...", delay)
        await asyncio.sleep(delay)
        delay = min(delay * RECONNECT_BACKOFF, RECONNECT_MAX_DELAY)


# =============================================================================
# Collecteur AISHub (polling HTTP toutes les 60s)
# =============================================================================

async def poll_aishub() -> None:
    """
    Interroge l'API HTTP d'AISHub toutes les AISHUB_POLL_INTERVAL secondes.
    AISHub a une couverture terrestre plus large qu'aisstream.io en France.
    Inscription gratuite sur https://www.aishub.net/
    """
    if not AISHUB_USERNAME or AISHUB_USERNAME.startswith("REMPLACE"):
        logger.info("[AISHub] Pas de username configuré — polling désactivé.")
        return

    logger.info("[AISHub] Polling démarré (intervalle %ds, MMSI %d)", AISHUB_POLL_INTERVAL, VESSEL_MMSI)

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                params = {
                    "username": AISHUB_USERNAME,
                    "format": "1",
                    "output": "json",
                    "compress": "0",
                    "mmsi": str(VESSEL_MMSI),
                }
                async with session.get(
                    AISHUB_API_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        logger.warning("[AISHub] HTTP %d", resp.status)
                        await asyncio.sleep(AISHUB_POLL_INTERVAL)
                        continue

                    raw = await resp.json(content_type=None)

                    # AISHub renvoie un tableau ; le 1er élément contient les infos de la requête,
                    # le 2ème contient les vessels
                    if not isinstance(raw, list) or len(raw) < 2:
                        logger.debug("[AISHub] Réponse vide ou inattendue : %s", raw)
                        await asyncio.sleep(AISHUB_POLL_INTERVAL)
                        continue

                    vessels = raw[1] if isinstance(raw[1], list) else []
                    if not vessels:
                        logger.debug("[AISHub] Aucune position pour MMSI %d", VESSEL_MMSI)
                        await asyncio.sleep(AISHUB_POLL_INTERVAL)
                        continue

                    v = vessels[0]
                    lat = float(v.get("LATITUDE", 0))
                    lon = float(v.get("LONGITUDE", 0))

                    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180) or (lat == 0 and lon == 0):
                        await asyncio.sleep(AISHUB_POLL_INTERVAL)
                        continue

                    # Timestamp AISHub : "2025-02-22 14:30:00"
                    raw_time = v.get("TIME", "")
                    try:
                        ts = datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                        timestamp = ts.isoformat()
                    except ValueError:
                        timestamp = datetime.now(timezone.utc).isoformat()

                    sog = v.get("SOG")
                    cog = v.get("COG")
                    heading = v.get("HEADING")
                    if heading is not None and int(heading) in (511, 0):
                        heading = None

                    nav_code = v.get("NAVSTAT")
                    nav_status = parse_nav_status(nav_code)
                    ship_name = v.get("NAME", f"MMSI:{VESSEL_MMSI}").strip()

                    save_position(
                        timestamp, lat, lon,
                        float(sog) if sog is not None else None,
                        float(cog) if cog is not None else None,
                        float(heading) if heading is not None else None,
                        nav_status,
                    )

                    logger.info(
                        "[AISHub] [%s] %s — %.4f°N %.4f°E — %.1f kn — Cap %.0f° — %s",
                        timestamp[:19], ship_name, lat, lon,
                        float(sog) if sog else 0.0,
                        float(cog) if cog else 0.0,
                        nav_status,
                    )

            except aiohttp.ClientError as e:
                logger.warning("[AISHub] Erreur réseau : %s", e)
            except Exception as e:  # noqa: BLE001
                logger.error("[AISHub] Erreur inattendue : %s", e)

            await asyncio.sleep(AISHUB_POLL_INTERVAL)


# =============================================================================
# Point d'entrée — les deux sources tournent en parallèle
# =============================================================================

async def main() -> None:
    """Lance aisstream.io (websocket) et AISHub (polling) en parallèle."""
    tasks = [
        asyncio.create_task(run_ais_collector(), name="aisstream"),
        asyncio.create_task(poll_aishub(), name="aishub"),
    ]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    logger.info("=== SailTracker AIS Collector démarré (aisstream.io + AISHub) ===")
    init_db()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Arrêt demandé (SIGINT)")
