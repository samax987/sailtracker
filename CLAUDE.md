# SailTracker — CLAUDE.md

Guide de développement pour Claude Code sur ce projet.

## Contexte

Système de surveillance et de planification de traversées en voilier.
- **Bateau** : POLLEN 1 (voilier)
- **Navigateur** : Samuel Demacedo
- **VPS** : 45.55.239.73 (DigitalOcean Ubuntu)
- **Répertoire** : `/var/www/sailtracker/`

## Stack technique

| Couche | Techno |
|--------|--------|
| Backend | Python 3.12 / Flask 3.1 |
| Base de données | SQLite (`sailtracker.db`) |
| Frontend | SPA Vanilla JS + Leaflet.js + Chart.js |
| Moteur routing | Rust (isochrones, `engine/`) |
| Météo | Open-Meteo API (vent, marine, ensemble ECMWF) |
| Tracking | AIS via aisstream.io WebSocket + Garmin InReach KML |
| Serveur | nginx → Flask (port 8085) |

## Commandes essentielles

```bash
# Redémarrer le serveur web
systemctl restart sailtracker-web

# Voir les logs en direct
journalctl -u sailtracker-web -f
tail -f /var/www/sailtracker/logs/passage.log

# Lancer le passage planner manuellement (route ID=26)
cd /var/www/sailtracker && venv/bin/python3 passage_planner.py --route-id 26

# Vérifier la DB
sqlite3 sailtracker.db "PRAGMA integrity_check"

# Status des services
systemctl status sailtracker-web sailtracker-ais
```

## Architecture fichiers

```
server.py              # Flask principal — 2200+ lignes, toutes les routes API
passage_planner.py     # Calcul météo multi-modèles par waypoint (cron 6h)
weather_collector.py   # Collecte Open-Meteo + Copernicus (cron 3h)
ais_collector.py       # WebSocket AIS temps réel
inreach_collector.py   # Polling KML Garmin InReach (cron 10min)
grib_collector.py      # Téléchargement GRIB GFS NOAA (cron 4×/jour)
forecast_verifier.py   # Vérification précision modèles (cron 6h10)
polar_calibrator.py    # Calibration auto polaires (cron horaire)
routing.py             # Algorithme isochrones Python (fallback Rust)
polars.py              # Polaires POLLEN 1 via scipy interpolation
daily_briefing.py      # Briefing météo quotidien (cron 7h UTC)
config.py              # Constantes partagées — importer depuis ici
engine/                # Moteur Rust (isochrones rapides)
data/polars/           # CSV polaires POLLEN 1
static/                # HTML/JS frontend (passage.html, index.html)
logs/                  # Logs rotatifs par module
```

## Base de données — tables principales

| Table | Rôle |
|-------|------|
| `positions` | GPS bateau (AIS + InReach) |
| `passage_routes` | Routes de traversée avec phases (planning/active/completed) |
| `passage_forecasts` | Prévisions météo par waypoint/heure |
| `departure_simulations` | Fenêtres de départ simulées (scores conf/comfort) |
| `ensemble_forecasts` | 51 membres ECMWF (~80% de la DB, volumineuse) |
| `weather_snapshots` | Snapshots météo horaires |
| `model_accuracy` | Erreurs prévision par modèle/zone/horizon |
| `polar_matrix` | Polaires calibrées SQLite |
| `polar_observations` | Observations TWA/TWS/STW pour calibration |
| `route_optimizations` | Résultats isochrones Rust |

## Workflow traversée (phases)

Les routes ont un champ `phase` : `planning` → `active` → `completed`

- **planning** : Simulation fenêtres de départ, optimisation route
- **active** : Traversée en cours — météo aux prochains waypoints affichée
- **completed** : Bilan — durée réelle, distance, vitesse moyenne

Champs DB associés : `actual_departure`, `actual_arrival`, `departure_port`, `arrival_port`, `notes`

## APIs importantes

```
GET  /api/position/latest              # Dernière position GPS
GET  /api/routes                       # Liste routes (inclut phase/dates)
GET  /api/passage/<id>/info            # Détails route + ETA polaire
POST /api/passage/<id>/compute         # Lancer calcul météo waypoints
GET  /api/passage/<id>/departures      # Fenêtres de départ simulées
GET  /api/passage/<id>/forecast        # Prévisions par waypoint
GET  /api/passage/<id>/active-weather  # Météo prochains WP (traversée active)
GET  /api/passage/<id>/completed-summary # Bilan traversée terminée
POST /api/passage/<id>/start           # Démarrer traversée (→ active)
POST /api/passage/<id>/arrive          # Enregistrer arrivée (→ completed)
GET  /api/passage/<id>/ensemble        # 51 membres ECMWF par WP
POST /api/passage/routes/<id>/optimize # Routage isochrones
```

## Crons actifs

| Schedule | Commande | Log |
|----------|----------|-----|
| `5 */3 * * *` | `weather_collector.py` | `logs/weather.log` |
| `*/10 * * * *` | `inreach_collector.py` | `logs/inreach.log` |
| `30 5,11,17,23 * * *` | `grib_collector.py` | `logs/grib.log` |
| `10 6 * * *` | `forecast_verifier.py` | `logs/verifier.log` |
| `15 * * * *` | `polar_calibrator.py` | `logs/calibration.log` |
| `*/30 * * * *` | `watchdog.py` | `logs/watchdog.log` |
| `0 7 * * *` | `daily_briefing.py` | `logs/briefing.log` |
| `0 */6 * * *` | `passage_planner.py` (toutes routes) | `logs/passage.log` |

> ⚠️ Le `passage_planner` cron ne tourne que si des routes en phase `planning` existent.
> Pour forcer : `venv/bin/python3 passage_planner.py --route-id <id>`

## Conventions de développement

### Python
- Utiliser `config.py` pour les constantes partagées (ne pas redéfinir `DB_PATH`, `BASE_DIR`, etc.)
- Toujours `load_dotenv(BASE_DIR / ".env")` avant d'utiliser `os.getenv()`
- Connexions SQLite : `conn.row_factory = sqlite3.Row` pour accès par nom de colonne
- Logs : `logging.getLogger(__name__)` — ne pas utiliser `print()`
- HTTP : réutiliser la session partagée `_SESSION = requests.Session()` avec User-Agent

### Frontend (JS)
- SPA vanilla — pas de build, édition directe dans `static/`
- `routeId` et `routeInfo` sont les variables globales de la route courante
- Après changement de route, toujours appeler `updatePhaseUI(phase)` + le loader de phase
- Variables CSS dans `:root` — utiliser `var(--accent)`, `var(--text-dim)`, etc.

### Sécurité
- `.env` : mode 600, owner www-data — ne jamais committer
- Pas de secret dans le code — tout via `os.getenv()`
- Uploads limités (voir `server.py`) — toujours valider côté serveur

## Historique récent

- **2026-04-05** : Ajout workflow 3 phases (planning/active/completed) dans `server.py` et `passage.html`
- **2026-04-05** : Traversée Cap-Vert → Saint-Martin enregistrée (13 mars – 2 avril 2026, 2030 NM, 20.2j)
- **2026-03-27** : Routage isochrones Rust + calibration polaires manuelle
- **2026-03-10** : Audit sécurité round 7 — defusedxml, upload limit
- **2026-03-08** : BLUEPRINT v1.4, audit rounds 4+5

## Points d'attention

1. `ensemble_forecasts` grossit vite (51 membres × waypoints × 6h) — surveiller taille DB
2. Le watchdog vérifie le passage_planner toutes les 30min — normal qu'il alerte si aucune route active
3. `server.py` est monolithique (2200+ lignes) — les nouvelles API vont à la fin, avant `if __name__ == '__main__'`
4. Le moteur Rust (`engine/`) est optionnel — fallback Python transparent si binaire absent
5. Les polaires POLLEN 1 sont dans `data/polars/pollen1.csv` — ne pas écraser sans backup
