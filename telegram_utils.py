#!/usr/bin/env python3
"""
telegram_utils.py — Utilitaire partagé pour l'envoi de messages Telegram.
Remplace les fonctions send_telegram() dupliquées dans daily_briefing,
passage_planner et watchdog.
"""
import logging
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "SailTracker/1.0 (telegram)"})

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def send_telegram(text: str, parse_mode: str = "HTML") -> bool:
    """Envoie un message Telegram. Retourne True si succès."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram non configuré — message ignoré")
        return False
    try:
        resp = _SESSION.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error("Erreur Telegram : %s", e)
        return False
