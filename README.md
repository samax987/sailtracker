# SailTracker

Système de suivi et de planification de traversée atlantique pour voilier.
Développé pour la traversée Cap-Vert → Barbade, utilisable pour tout passage hauturier.

## Fonctionnalités

- **Tracking temps réel** : position AIS (aisstream.io) + Garmin InReach, trace sur carte Leaflet
- **Passage Planner** : simulation de 15 fenêtres de départ sur 15 jours, scoring multi-critères (vent, vagues, houle, courants)
- **Polaires** : polaires POLLEN 1 intégrées (scipy interpolation), calibration automatique via positions réelles
- **Ensemble ECMWF** : 51 membres téléchargés via NOAA GFS GRIB2, graphique spaghetti P10/P90 + tube de confiance
- **Météo multi-sources** : Open-Meteo (vent + vagues), Copernicus Marine (courants), NOAA GFS (GRIB2 animé)
- **Mode en mer** : détection automatique navigation active, bandeau progression/ETA, ETA basée sur vitesse réelle 6h
- **Alertes Telegram** : fenêtre de départ détectée, résumé quotidien 07h UTC (pré-départ ou en mer), watchdog système
- **Import/Export** : GPX (routes + tracks), KML, KMZ (Navionics, OpenCPN, Garmin)
- **Vérification précision modèles** : erreur MAE par modèle sur 30 jours glissants
- **Watchdog** : surveillance Flask/DB/disque/RAM toutes les 30 min, maintenance DB automatique

## Stack technique

| Composant | Technologie |
|-----------|-------------|
| Backend | Python 3.12 / Flask 3.1 |
| Base de données | SQLite (10 tables) |
| Carte | Leaflet.js + leaflet-velocity (overlay GRIB animé) |
| Graphiques | Chart.js |
| Calcul routage | NumPy / SciPy (isochrones + interpolation polaires) |
| Météo | Open-Meteo API, Copernicus Marine, NOAA GFS GRIB2 |
| Tracking | aisstream.io WebSocket, Garmin InReach MapShare KML |
| Alertes | Telegram Bot API |
| Déploiement | systemd (2 services) + cron (8 tâches) |

## Structure des fichiers

```
sailtracker/
├── server.py               # Serveur Flask principal (~2000 lignes, 25+ endpoints)
├── passage_planner.py      # Planner multi-modèles + ensembles (832 lignes)
├── weather_collector.py    # Collecte Open-Meteo + Copernicus (cron 3h)
├── grib_collector.py       # Téléchargement GRIB GFS NOAA (cron 6h)
├── inreach_collector.py    # Polling KML Garmin InReach (cron 10min)
├── ais_collector.py        # Tracking AIS via WebSocket (service systemd)
├── forecast_verifier.py    # Vérification précision modèles (cron quotidien)
├── polar_calibrator.py     # Calibration polaires via positions réelles (cron 1h)
├── daily_briefing.py       # Résumé Telegram quotidien (cron 07h UTC)
├── watchdog.py             # Surveillance + maintenance DB (cron 30min)
├── briefing.py             # Génération briefing météo textuel
├── routing.py              # Algorithme isochrones
├── polars.py               # Polaires POLLEN 1 + scipy
├── config.py               # Constantes partagées
├── utils.py                # Utilitaires
├── requirements.txt
├── .env.example
│
├── static/
│   ├── index.html          # Tracker temps réel (SPA Leaflet)
│   ├── passage.html        # Passage Planner (SPA Chart.js + Leaflet)
│   └── lib/                # Leaflet, Chart.js, leaflet-velocity (local)
│
├── templates/
│   ├── base.html
│   ├── accuracy.html       # Précision modèles météo
│   ├── polars.html         # Polaires POLLEN 1
│   └── passage_lite.html   # Vue légère basse bande passante
│
└── data/
    └── polars/
        └── pollen1_default.csv  # Polaires par défaut POLLEN 1
```

## Installation

### Prérequis

- Python 3.12+
- Ubuntu/Debian (ou tout Linux avec systemd)
- 1 GB RAM minimum, 10 GB disque

### Cloner et installer

```bash
git clone https://github.com/samax987/sailtracker.git
cd sailtracker

# Environnement virtuel
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configuration
cp .env.example .env
nano .env  # Remplir les variables (voir section Configuration)

# Démarrer le serveur (crée sailtracker.db automatiquement)
python server.py
```

### Service systemd

```ini
# /etc/systemd/system/sailtracker-web.service
[Unit]
Description=SailTracker Web Server (Flask)
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/var/www/sailtracker
ExecStart=/var/www/sailtracker/venv/bin/python /var/www/sailtracker/server.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable sailtracker-web
systemctl start sailtracker-web
```

### Crontab

```cron
# Weather collector (toutes les 3h)
0 */3 * * * /path/venv/bin/python /path/weather_collector.py

# InReach polling (toutes les 10 min)
*/10 * * * * /path/venv/bin/python /path/inreach_collector.py

# Passage Planner (toutes les 6h)
0 */6 * * * /path/venv/bin/python /path/passage_planner.py

# GRIB GFS — 3h30 après run NOAA 00/06/12/18z
30 5,11,17,23 * * * /path/venv/bin/python /path/grib_collector.py

# Forecast Verifier (06h UTC)
0 6 * * * /path/venv/bin/python /path/forecast_verifier.py

# Polar Calibrator (toutes les heures)
0 * * * * /path/venv/bin/python /path/polar_calibrator.py

# Daily Telegram Briefing (07h UTC)
0 7 * * * /path/venv/bin/python /path/daily_briefing.py

# Watchdog + maintenance DB (toutes les 30 min)
*/30 * * * * /path/venv/bin/python /path/watchdog.py
```

## Configuration

Copier `.env.example` en `.env` et remplir les valeurs :

| Variable | Description | Requis |
|----------|-------------|--------|
| `AISSTREAM_API_KEY` | Clé API [aisstream.io](https://aisstream.io) | Oui |
| `VESSEL_MMSI` | MMSI du voilier (9 chiffres) | Oui |
| `VESSEL_NAME` | Nom du voilier | Oui |
| `INREACH_KML_URL` | URL KML MapShare Garmin InReach | Oui |
| `TELEGRAM_BOT_TOKEN` | Token bot Telegram (@BotFather) | Oui |
| `TELEGRAM_CHAT_ID` | Chat ID Telegram | Oui |
| `SERVER_URL` | URL publique du serveur | Oui |
| `COPERNICUS_USER` | Login Copernicus Marine | Non |
| `COPERNICUS_PASS` | Mot de passe Copernicus Marine | Non |
| `FLASK_HOST` | Adresse d'écoute Flask (défaut: 127.0.0.1) | Non |
| `FLASK_PORT` | Port Flask (défaut: 8085) | Non |

## API

| Endpoint | Méthode | Description |
|----------|---------|-------------|
| `/` | GET | Tracker temps réel |
| `/passage` | GET | Passage Planner |
| `/api/position` | GET | Dernière position bateau |
| `/api/at-sea` | GET | Mode en mer : progression, ETA, météo |
| `/api/weather/current` | GET | Conditions météo actuelles |
| `/api/passage/<id>/ensemble` | GET | Ensemble ECMWF 51 membres |
| `/api/gpx/parse` | POST | Import GPX / KML / KMZ |
| `/api/health` | GET | Health check |

## Licence

MIT
