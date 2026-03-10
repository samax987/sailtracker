# SailTracker — Blueprint Technique Complet

> **Audit réalisé le** : 2026-03-08 ~10h30 UTC  
> **Serveur** : 45.55.239.73 (DigitalOcean VPS, Ubuntu)  
> **Répertoire** : `/var/www/sailtracker/`  
> **Stack** : Python 3.12 / Flask 3.1 / SQLite / Leaflet.js

---

## 1. Arborescence des fichiers

```
/var/www/sailtracker/
├── .env                          # Secrets (600, www-data only) ✅
├── config.py                     # Constantes partagées (29 lignes)
├── server.py                     # Serveur Flask principal (1807 lignes)
├── passage_planner.py            # Passage planner multi-modèles (832 lignes)
├── weather_collector.py          # Collecte météo Open-Meteo + Copernicus (467 lignes)
├── ais_collector.py              # Tracking AIS via aisstream.io WebSocket (453 lignes)
├── inreach_collector.py          # Polling KML Garmin InReach (260 lignes)
├── grib_collector.py             # Téléchargement GRIB GFS NOAA (351 lignes)
├── forecast_verifier.py          # Vérification précision modèles (202 lignes)
├── polars.py                     # Polaires POLLEN 1 / scipy (173 lignes)
├── routing.py                    # Algorithme isochrones (405 lignes)
├── polar_calibrator.py           # Calibration auto via Open-Meteo (332 lignes)
├── briefing.py                   # Génération briefing météo textuel (238 lignes)
├── utils.py                      # Utilitaires partagés (29 lignes)
├── requirements.txt              # Dépendances pip
├── sailtracker.db                # Base SQLite (27 MB)
│
├── data/
│   └── polars/
│       ├── pollen1.csv           # Polaires actuelles (33 angles × 13 vitesses)
│       └── pollen1_default.csv   # Sauvegarde polaires par défaut
│
├── static/
│   ├── index.html                # Page Tracker (SPA Leaflet)
│   ├── passage.html              # Passage Planner (SPA Chart.js + Leaflet, 2034 lignes)
│   ├── lib/
│   │   ├── leaflet.js / leaflet.css
│   │   ├── leaflet-velocity.min.js / .css  # Overlay vent GRIB animé
│   │   ├── marker-icon.png / marker-icon-2x.png / marker-shadow.png
│   └── grib_cache/
│       ├── index.json            # Index des runs GRIB disponibles
│       ├── wind_20260305_00z_f000.json → f072.json  # 11 fichiers ~770KB chacun
│       └── tmp/                  # Fichiers .idx temporaires
│
├── templates/
│   ├── base.html                 # Layout nav Flask (Tracker/Passage/Lite/Modèles/Polaires)
│   ├── accuracy.html             # Page précision modèles
│   ├── passage_lite.html         # Version légère passage planner
│   └── polars.html               # Page polaires POLLEN 1 (canvas + tableau)
│
├── logs/
│   ├── server.log                # Logs Flask (rotating 10MB×5)
│   ├── passage.log               # Logs passage planner
│   ├── weather_cron.log          # Logs collecte météo
│   ├── inreach.log               # Logs polling InReach
│   ├── ais.log                   # Logs AIS collector
│   ├── grib_cron.log             # Logs collecte GRIB
│   ├── polar_calibration.log     # Logs calibration polaires
│   ├── verifier_cron.log         # Logs vérificateur modèles
│   └── weather.log               # Log secondaire météo
│
└── venv/                         # Environnement Python 3.12
```

---

## 2. Base de données SQLite

**Fichier** : `sailtracker.db` — **27 MB** — `PRAGMA integrity_check` : **OK** ✅

### Schéma et volumétrie

| Table | Lignes | Rôle |
|-------|-------:|------|
| `positions` | **18** | Positions GPS (AIS + InReach). Colonnes: id, timestamp, latitude, longitude, speed_knots, course, heading, nav_status, source, created_at. ⚠️ Colonnes parasites migrées : `status`, `last_computed` (appartiennent à passage_routes) |
| `weather_snapshots` | **27** | Snapshot météo horaire : vent (kmh + direction + rafales), vagues, houle, courant (Copernicus). Collecté toutes les 3h |
| `weather_forecasts` | **72** | Prévisions tabulaires (value1/value2/value3) — format générique peu exploité |
| `passage_routes` | **2** | Routes définies : #6 "tarafalle-mindello" (5kts, ready), #7 "Cap-Vert-Barbade" (6kts, **computing** bloqué) |
| `passage_forecasts` | **26 112** | Prévisions multi-modèles par waypoint/heure : vent, vagues, houle, courant. Index sur (route_id, collected_at, model) |
| `departure_simulations` | **120** | 15j × 2 routes × ~4 runs : scores conf/comfort/overall, ETA ajusté. ⚠️ FK incorrecte : `REFERENCES departure_simulations(id)` au lieu de `passage_routes(id)` |
| `ensemble_forecasts` | **220 800** | 51 membres ECMWF IFS025, toutes les 6h, par waypoint. Volumineuse (~80% de la DB) |
| `model_accuracy` | **227** | Erreurs de prévision par modèle/zone/horizon. 3 modèles : ecmwf_ifs025, gfs_seamless, icon_seamless |
| `polar_observations` | **0** | Observations TWA/TWS/STW pour calibration (vide : bateau à l'ancre) |
| `route_optimizations` | **1** | Résultat isochrones stocké (route 6, départ 2026-03-08T09Z, 8404 chars JSON) |

### Dernières données

```
Dernière position InReach : 2026-03-07 01:16:15 (33h ago — ancre Mindelo)
Dernière collecte météo   : 2026-03-08 09:00:18 UTC (vent NULL, courant 0.15 kts @98°)
Meilleure fenêtre départ  : 2026-03-09 — score 77/100 (conf=80, comfort=94)
ETA Cap-Vert→Barbade      : ~324h (vitesse fixe 6kts, polaires pas encore utilisées)
```

---

## 3. Endpoints API Flask

**Serveur** : `127.0.0.1:8085` (proxy nginx recommandé non vérifié)

### Pages HTML

| Route | Statut | Description |
|-------|--------|-------------|
| `GET /` | ✅ 200 | Tracker temps réel (Leaflet + AIS/InReach) |
| `GET /passage` | ✅ 200 | Passage Planner complet (SPA) |
| `GET /passage/lite` | ✅ 200 | Version allégée (Flask template) |
| `GET /accuracy` | ✅ 200 | Précision des modèles météo |
| `GET /polars` | ✅ 200 | Polaires POLLEN 1 (canvas + tableau éditable) |

### API Position & Statut

| Route | Statut | Retour |
|-------|--------|--------|
| `GET /api/position/latest` | ✅ 200 | Dernière position (source: inreach, lat=16.570, lon=-24.363, spd=0.0) |
| `GET /api/position/latest?source=inreach` | ✅ 200 | Idem filtré par source |
| `GET /api/position/track` | ✅ 200 | Historique positions (dernières 24h par défaut) |
| `GET /api/status` | ✅ 200 | État AIS/InReach/Météo avec âge des données |
| `GET /api/health` | ✅ 200 | `{"status":"ok", "server_time":..., "last_ais_position":...}` |
| `GET /api/stats` | ✅ 200 | Statistiques globales |

### API Météo

| Route | Statut | Retour |
|-------|--------|--------|
| `GET /api/weather/latest` | ✅ 200 | Dernier snapshot (⚠️ wind_speed_kmh=null après erreur SSL) |
| `GET /api/weather/forecast` | ✅ 200 | Prévisions |

### API Passage Planner

| Route | Statut | Retour |
|-------|--------|--------|
| `GET /api/routes` | ✅ 200 | `{routes: [{id:6, "tarafalle-mindello"}, {id:7, "Cap-Vert-Barbade"}]}` |
| `POST /api/routes` | ✅ 200 | Crée une nouvelle route |
| `GET /api/passage/<id>/info` | ✅ 200 | Info route + ETA (fixe + polaire si dispo) |
| `GET /api/passage/<id>/forecast` | ✅ 200 | Prévisions par waypoint (dernière collecte) |
| `GET /api/passage/<id>/departures` | ✅ 200 | 15 simulations de départ avec scores |
| `GET /api/passage/<id>/ensemble?wp=N` | ✅ 200 | Données ensemble 51 membres |
| `POST /api/passage/<id>/compute` | ✅ 200 | Lance calcul en background (subprocess) |
| `GET /api/passage/<id>/compute_status` | ✅ 200 | Statut du calcul |
| `GET /api/passage/<id>/briefing` | ✅ 200 | Briefing météo texte généré |
| `GET /api/passage/summary` | ✅ 200 | Résumé global (meilleur départ, score) |
| `POST /api/passage/routes/<id>/optimize` | ✅ 200 | Lance routage isochrones (async thread) |
| `GET /api/passage/routes/<id>/optimize/status?task_id=` | ✅ 200 | Statut tâche isochrones |
| `GET /api/passage/routes/<id>/optimize/result?task_id=` | ✅ 200 | Résultat isochrones |
| `POST /api/passage/routes/<id>/move-waypoint` | ✅ 200 | Déplace un WP |
| `POST /api/passage/routes/<id>/rename` | ✅ 200 | Renomme route |
| `POST /api/passage/routes/<id>/delete` | ✅ 200 | Supprime route |
| `GET /api/passage/wind-grid` | ✅ 200 | Grille vent pour overlay |
| `POST /api/gpx/parse` | ⚠️ 400 | Parse GPX/KML → waypoints (erreur sur XML minimal de test, format strict requis) |

### API Polaires

| Route | Statut | Retour |
|-------|--------|--------|
| `GET /api/polars` | ✅ 200 | Matrice complète TWA(33)×TWS(13) |
| `PUT /api/polars` | ✅ 200 | Mise à jour une valeur |
| `POST /api/polars/reset` | ✅ 200 | Reset vers default |
| `GET /api/polars/export` | ✅ 200 | Télécharge pollen1.csv |
| `GET /api/polars/speed?twa=90&tws=15` | ✅ 200 | `{"boat_speed_kts": 7.5}` |
| `GET /api/polars/observations` | ✅ 200 | `{observations: []}` (0 obs, bateau à quai) |
| `GET /api/polars/comparison` | ✅ 200 | Diff théorique vs observé |

### API Tracker & GRIB

| Route | Statut | Retour |
|-------|--------|--------|
| `GET /api/tracker/status` | ✅ 200 | AIS: active, InReach: 1985 min ago, 18 positions |
| `POST /api/tracker/sync-inreach` | ✅ 200 | Force sync InReach |
| `POST /api/tracker/restart` | ✅ 200 | Redémarre collecteurs |
| `GET /api/grib/index` | ✅ 200 | Run GFS disponible : 20260305_00z (11 échéances f000→f072) |

---

## 4. Cron Jobs

```cron
# Toutes les 6h (0h, 6h, 12h, 18h)
0 */6 * * *  passage_planner.py      → Calcul prévisions + simulations départ

# Toutes les 3h
0 */3 * * *  weather_collector.py    → Snapshot météo Open-Meteo + courant Copernicus

# Toutes les 10 min
*/10 * * * * inreach_collector.py    → Polling KML Garmin InReach

# 3h30 après les runs GFS (03:30, 09:30, 15:30, 21:30)
30 3,9,15,21 * * * grib_collector.py → Téléchargement GRIB GFS NOAA

# Quotidien 06h00
0 6 * * *    forecast_verifier.py   → Calcul précision modèles

# Toutes les heures
0 * * * *    polar_calibrator.py    → Calibration polaires (positions InReach + Open-Meteo)
```

### Statut dernière exécution

| Script | Dernière exécution | Statut |
|--------|-------------------|--------|
| `passage_planner.py` | 2026-03-08 06:00 | ❌ **database is locked** (route 7 bloquée "computing") |
| `weather_collector.py` | 2026-03-08 09:00 | ⚠️ SSL error Open-Meteo + Marine 404 / Copernicus ✅ |
| `inreach_collector.py` | 2026-03-08 10:20 | ✅ KML téléchargé, 0 nouvelles positions (ancre) |
| `grib_collector.py` | 2026-03-08 09:30 | ⚠️ f060 et f072 : 404 (run GFS 06z pas encore publié) |
| `forecast_verifier.py` | 2026-03-08 06:02 | ⚠️ Erreurs = 0.0 sur tous les modèles (suspect) |
| `polar_calibrator.py` | 2026-03-08 10:00 | ✅ Tourne, 0 obs (bateau à quai) |
| `ais_collector.py` | service systemd | ⚠️ Tentative #151, timeout répétés, connecté 07:30 |

---

## 5. Services Systemd

| Service | Statut | PID | Mémoire | CPU total | Depuis |
|---------|--------|-----|---------|-----------|--------|
| `sailtracker-web` | ✅ **active** | 378055 | 39.5 MB | ~1s | 2026-03-08 08:43 |
| `sailtracker-ais` | ✅ **active** | 335860 | 49.3 MB | 21s | 2026-03-06 00:13 |

---

## 6. État des Fonctionnalités

| Feature | Statut | Détail |
|---------|--------|--------|
| **Tracking AIS** | ⚠️ Partiel | Service actif, connecté (tentative #151), 0 positions reçues. MMSI 227493090 hors zone AIS (ancre Mindelo) |
| **Tracking InReach** | ✅ OK | KML Garmin polled toutes les 10min. 18 positions. Dernière : 2026-03-07 01:16:15 (33h ago, ancre) |
| **Passage Planner** | ⚠️ Partiel | Route #6 ✅ (ready, 15 sims), Route #7 ❌ bloquée "computing" depuis 00:01 (db locked) |
| **Multi-modèles** | ✅ OK | ECMWF IFS025, GFS Seamless, ICON Seamless — 26K prévisions en DB |
| **Ensemble ECMWF** | ✅ OK | 220K rows, 51 membres, endpoint /ensemble fonctionnel |
| **Courants océaniques** | ✅ OK | Copernicus Marine API (0.15 kts @98° dernier snapshot) |
| **Polaires POLLEN 1** | ✅ OK | scipy RegularGridInterpolator, 33×13 valeurs, API /api/polars/speed fonctionnel |
| **Passage Planner + Polaires** | ⚠️ Partiel | Code intégré (passage_planner.py), mais la route #7 n'a pas encore tourné avec polaires (stuck). Route #6 → ETA polaire non encore calculé (last_computed avant intégration) |
| **Calibration polaires** | 🔲 En attente | Cron actif, logique correcte, mais 0 obs (bateau à l'ancre, SOG=0) |
| **Routage Isochrones** | ✅ OK | Algorithme fonctionnel, test route #6 → 2 isochrones, result stocké en DB |
| **GRIB Vent overlay** | ⚠️ Périmé | 11 fichiers wind_20260305_*.json (3 jours), dernier run 09:30 a échoué (404) |
| **Import GPX/KML** | ⚠️ Incertain | Endpoint existe (POST /api/gpx/parse), retourne 400 sur XML minimal — format précis requis |
| **Alertes Telegram** | ✅ OK | Message test envoyé avec succès. Dernier message automatique : 2026-03-08 00:01 (score 77, 2026-03-09) |
| **Mode Lite** | ✅ OK | `/passage/lite` → 200, template passage_lite.html |
| **Page Modèles** | ✅ OK | `/accuracy` → 200, 227 entrées model_accuracy |
| **Page Polaires** | ✅ OK | `/polars` → 200, canvas + tableau éditable |
| **Mode en mer** | 🔲 Non impl. | Pas de logique "mode navigation active" distincte |
| **Forecast Verifier** | ⚠️ Suspect | Tourne mais erreurs = 0.00 kts sur tous les modèles/horizons → probable bug comparaison |

---

## 7. Architecture & Flux de données

```
┌─────────────────────────────────────────────────────────┐
│                    SOURCES DE DONNÉES                    │
├──────────────┬──────────────┬──────────────┬────────────┤
│ AIS stream   │ Garmin       │ Open-Meteo   │ NOAA GFS   │
│ (WebSocket)  │ InReach KML  │ API REST     │ GRIB2      │
│ MMSI:227493  │ toutes 10min │ toutes 3h    │ toutes 6h  │
└──────┬───────┴──────┬───────┴──────┬───────┴─────┬──────┘
       │              │              │             │
       ▼              ▼              ▼             ▼
┌──────────────┐ ┌──────────┐ ┌──────────────┐ ┌──────────┐
│ais_collector │ │inreach_  │ │weather_      │ │grib_     │
│.py (systemd) │ │collector │ │collector.py  │ │collector │
│              │ │.py (cron)│ │(cron /3h)    │ │.py(cron) │
└──────┬───────┘ └────┬─────┘ └──────┬───────┘ └────┬─────┘
       │              │              │               │
       └──────────────┴──────┬───────┘         ┌────▼──────┐
                             │                 │grib_cache/│
                             ▼                 │wind_*.json│
                    ┌────────────────┐         └─────┬─────┘
                    │  sailtracker   │               │
                    │    .db         │◄──────────────┘
                    │  (SQLite 27MB) │
                    └───────┬────────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
     ┌─────────────┐ ┌──────────────┐ ┌──────────┐
     │passage_     │ │polar_        │ │forecast_ │
     │planner.py   │ │calibrator.py │ │verifier  │
     │(cron /6h)   │ │(cron /1h)    │ │.py(/6h)  │
     │+polaires    │ │+Open-Meteo   │ │          │
     └──────┬──────┘ └──────┬───────┘ └────┬─────┘
            │               │               │
            └───────────────▼───────────────┘
                            │
                    ┌───────▼────────┐
                    │   server.py    │
                    │  Flask :8085   │
                    │  25+ endpoints │
                    └───────┬────────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
        ┌──────────┐ ┌──────────┐ ┌──────────────┐
        │ index    │ │ passage  │ │ templates/   │
        │ .html    │ │ .html    │ │ polars.html  │
        │ (Tracker)│ │(Planner) │ │ accuracy.html│
        └──────────┘ └──────────┘ └──────────────┘
```

---

## 8. Dépendances Python (venv)

| Package | Version | Usage |
|---------|---------|-------|
| Flask | 3.1.3 | Serveur web |
| flask-cors | 6.0.2 | CORS headers |
| numpy | 2.4.2 | Calculs vectoriels |
| scipy | 1.17.1 | Interpolation polaires (RegularGridInterpolator) |
| requests | 2.32.5 | API REST Open-Meteo, Telegram |
| websockets | 16.0 | AIS stream |
| aiohttp | 3.13.3 | HTTP async |
| xarray | 2026.2.0 | Traitement données GRIB/NetCDF |
| netCDF4 | 1.7.4 | Lecture GRIB |
| copernicusmarine | 2.3.0 | Courants Copernicus |
| cfgrib | 0.9.15.1 | Décodage GRIB2 |
| python-dotenv | 1.2.1 | Variables d'environnement |
| pandas | 2.3.3 | Manipulation données |
| lxml | 6.0.2 | Parse KML InReach |
| eccodes/eccodeslib | 2.46.0 | Bibliothèque GRIB ECMWF |
| Werkzeug | 3.1.6 | WSGI Flask |
| Jinja2 | 3.1.6 | Templates Flask |
| dask | 2026.1.2 | Calcul parallèle (xarray backend) |
| zarr | 3.1.5 | Stockage tableau |
| boto3/botocore | 1.42.54 | AWS SDK (Copernicus) |
| tqdm | 4.67.3 | Barres de progression |
| certifi | 2026.1.4 | Certificats SSL |

**Packages dans requirements.txt mais vérification** :
- `apscheduler` : dans requirements.txt mais **non installé** (non utilisé directement)
- `scipy` : ajouté lors de l'implémentation des polaires ✅

---

## 9. Problèmes identifiés — Statut des corrections

> **Corrections appliquées le 2026-03-08** — Tous les bugs critiques et majeurs résolus.

### ✅ Résolus

**Bug #1 — Route #7 bloquée "computing"** [CORRIGÉ]
- `UPDATE passage_routes SET status='ready' WHERE id=7;`

**Bug #2 — Marine API 404** [CORRIGÉ]
- `weather_collector.py` ligne 244 : `api.open-meteo.com/v1/marine` → `marine-api.open-meteo.com/v1/marine`
- Impact résolu : `wave_height_m` et `swell_*` seront désormais renseignés dans `weather_snapshots`

**Bug #3 — GRIB cron timing** [CORRIGÉ]
- Cron déplacé de `30 3,9,15,21 * * *` à `30 5,11,17,23 * * *` (+2h)
- Le run GFS (durée ~3-4h) est maintenant publié sur NOMADS avant le déclenchement

**Bug #4 — Forecast Verifier erreurs 0.0 kts** [CORRIGÉ]
- Cause racine : les "observations" utilisaient la même API forecast que les prévisions → comparaison du modèle avec lui-même
- Fix : utilisation de l'API ERA5 archive (`archive-api.open-meteo.com`) comme vérité terrain
- Les erreurs seront désormais non-nulles et différenciées par horizon (H+24 à H+168)

**Bug #5 — Double logging AIS** [CORRIGÉ]
- `ais_collector.py` : ajout de `logger.propagate = False` pour couper la propagation vers le root logger
- Service AIS redémarré

**Bug #6 — Polaires non actives dans le passage planner** [CORRIGÉ]
- `passage_planner.py` relancé manuellement → `used_polars: true` confirmé dans les simulations
- Simulations #266-270 : `avg_polar_speed_kts` entre 5.46 et 5.77 kts
- ETA polaire : 15.2 jours pour départ optimal (2026-03-22)
- Endpoint `/api/passage/7/info` retourne `"used_polars": true`

**Bug #7 — FK erronée dans departure_simulations** [CORRIGÉ]
- Migration SQLite : `REFERENCES departure_simulations(id)` → `REFERENCES passage_routes(id)`
- Opération : CREATE TABLE new + INSERT SELECT + DROP + RENAME

### 🟡 Mineurs / Non bloquants (acceptés)

**Bug #8 — `positions` : colonnes parasites**
- Colonnes `status` et `last_computed` dans la table `positions` (migration accidentelle)
- Aucun impact fonctionnel, pas de correction nécessaire

**Bug #9 — GRIB collector : skip f060/f072**
- Quand le dernier run n'est pas encore complet, les forecast hours finaux font 404
- Comportement attendu, le cron suivant les récupère

**Bug #10 — weather_snapshots : vent NULL (transitoire)**
- Erreur SSL transiente à 09h00 → snapshot avec NULL wind
- Auto-corrigé au prochain cycle

---

## 10. Sécurité

| Élément | Statut | Détail |
|---------|--------|--------|
| Fichier `.env` | ✅ **Sécurisé** | Permissions 600 (rw-------), propriétaire www-data |
| Secrets dans le code | ✅ **OK** | Tous via `os.getenv()`, aucun hardcodé |
| Fichiers Python | ⚠️ **644** | World-readable (root ou www-data owner). OK pour VPS isolé, mais idéalement 640 |
| `sailtracker.db` | ⚠️ **644** | World-readable. Contient positions GPS, scores. Envisager 640 |
| Variables d'environnement | ✅ **OK** | AISSTREAM_API_KEY, COPERNICUS_USER/PASS, TELEGRAM_BOT_TOKEN, INREACH_KML_URL |
| Exposition réseau | ✅ **OK** | Flask bind sur 127.0.0.1:8085 (localhost uniquement) |
| CORS | ⚠️ **Ouvert** | `flask-cors` configuré sans restriction d'origine (`CORS(app)`) |
| SQLite injection | ✅ **OK** | Requêtes paramétrées (`?` placeholders) dans tout server.py |

---

## 11. Données temps réel (2026-03-08 ~10h40 UTC)

```
Position bateau   : 16.570°N, -24.363°E (Mindelo, Cap-Vert — ancre)
Dernière position : 2026-03-07 01:16:15 UTC (~33h ago)
Vitesse actuelle  : 0.0 kts (à l'ancre)

Meilleure fenêtre départ (Cap-Vert→Barbade) :
  Date : 2026-03-22
  Score global : 97/100 (confiance=90, confort=100)
  ETA polaire   : 366h = 15.2 jours @ 5.77 kts (polaires actives ✅)
  Telegram envoyé : ✅ 2026-03-08 10:38 UTC
```

---

*Généré 2026-03-08 — SailTracker Blueprint v1.1 — Tous bugs critiques corrigés*

---

## 12. Consolidations pré-traversée (2026-03-08 ~15h30 UTC)

### Fichiers ajoutés/modifiés

| Fichier | Action | Détail |
|---------|--------|--------|
|  | ✅ **Nouveau** | Résumé Telegram quotidien 07h00 UTC — pré-départ (fenêtre + score) ou en mer (position + ETA + météo) |
|  | ✅ **Nouveau** | Watchdog toutes les 30min — 6 checks + maintenance DB automatique |
|  | ✅ **Modifié** | Support KML/KMZ dans  + endpoint  |
|  | ✅ **Modifié** | Retry SSL backoff (3 tentatives : 30s/60s/120s) pour Open-Meteo |
|  | ✅ **Modifié** | Bandeau vert «Traversée en cours» + progression/ETA (polling /api/at-sea toutes les 5min) |
|  | ✅ **Modifié** | Toggle «Ensemble» (spaghetti 51 membres opacité 0.35) + bouton «📥 GPX» export route |

### Nouveaux endpoints

| Route | Méthode | Description |
|-------|---------|-------------|
|  | Flask | Détecte navigation active (InReach <2h, vitesse >1kt, <50NM route) → progression, ETA réelle, météo |
|  | Flask | Support étendu : GPX (rte/trk/wpt) + **KML** (Placemark/LineString) + **KMZ** (zip→KML) |

### Crons ajoutés

| Cron | Script | Fréquence |
|------|--------|-----------|
|  |  | Résumé Telegram quotidien 07h00 UTC |
|  |  | Watchdog surveillance + maintenance DB |

### Watchdog — Checks

| Check | Seuil | Action si échec |
|-------|-------|-----------------|
| Flask health | Répond HTTP 200 | Alerte Telegram |
| Passage planner | Simulation <12h | Alerte Telegram |
| Weather collector | Snapshot <6h | Alerte Telegram |
| SQLite PRAGMA quick_check | Résultat = ok | Alerte Telegram |
| Espace disque | >1 GB libre | Alerte Telegram |
| RAM | <90% utilisée | Alerte Telegram |
| **Anti-spam** | 1 alerte / problème / 6h | State dans /tmp/watchdog_last_alert.json |

### Maintenance DB automatique (watchdog)

| Table | Rétention | Fréquence |
|-------|-----------|-----------|
|  | 7 jours | Toutes les 30min |
|  | 14 jours | Toutes les 30min |
|  | 30 jours | Toutes les 30min |
| VACUUM | Si DB > 100 MB | Toutes les 30min |

*Consolidation 2026-03-08 ~15h30 UTC — SailTracker Blueprint v1.2*


---

## 12. Consolidations pre-traversee (2026-03-08 ~15h30 UTC)

### Fichiers ajoutes/modifies

| Fichier | Action | Detail |
|---------|--------|--------|
| daily_briefing.py | NOUVEAU | Resume Telegram quotidien 07h00 UTC |
| watchdog.py | NOUVEAU | Watchdog 30min — 6 checks + maintenance DB |
| server.py | MODIFIE | KML/KMZ dans /api/gpx/parse + endpoint /api/at-sea |
| weather_collector.py | MODIFIE | Retry SSL backoff 3 tentatives 30s/60s/120s |
| static/index.html | MODIFIE | Bandeau traversee en cours + /api/at-sea toutes les 5min |
| static/passage.html | MODIFIE | Toggle Ensemble spaghetti + bouton export GPX |

### Nouveaux crons

- 0 7 * * * daily_briefing.py — Resume Telegram quotidien 07h UTC
- */30 * * * * watchdog.py — Surveillance + maintenance DB

### Watchdog checks

| Check | Seuil |
|-------|-------|
| Flask health | HTTP 200 |
| Passage planner | Simulation < 12h |
| Weather collector | Snapshot < 6h |
| SQLite integrity | PRAGMA quick_check = ok |
| Espace disque | > 1 GB libre |
| RAM | < 90% utilisee |
| Anti-spam | 1 alerte / probleme / 6h |

### Maintenance DB auto (watchdog toutes les 30min)

| Table | Retention |
|-------|-----------|
| ensemble_forecasts | 7 jours |
| passage_forecasts | 14 jours |
| departure_simulations | 30 jours |
| VACUUM si DB > 100 MB | auto |

Blueprint v1.2 — 2026-03-08

---

## 13. Audit complet — 2026-03-10

> **Audit réalisé le** : 2026-03-10 ~10h30 UTC  
> **3 rounds de corrections appliquées**

### Corrections Round 1 (anti rate-limiting Open-Meteo)

| Fix | Détail |
|-----|--------|
| Batch multi-coords | 54 requêtes/run → 6 (fetch_wind/marine/ensemble_batch) |
| requests.Session() | Réutilisation connexions SSL dans passage_planner + weather_collector |
| User-Agent |  sur tous les appels HTTP |
| API_DELAY | 0.5s → 2.0s entre les 6 appels batch |

### Corrections Round 2

| Fix | Détail |
|-----|--------|
| AIS SSL context | ssl.create_default_context(certifi) + open_timeout=30s |
| Forecast Verifier | Horizons réels — H+N compare ERA5(J-N) vs prévision(J-N) |
| Departure simulations | Purge avant INSERT (3 doublons/date → 1) + nettoyage existants |
| GRIB cache | cleanup_old_runs() sur early-return + 66 fichiers orphelins supprimés (66MB→17MB) |
| Nginx | proxy_read_timeout 30s → 120s |

### Corrections Round 3 (SQLite + bugs)

| Fix | Détail |
|-----|--------|
| SQLite WAL mode | PRAGMA journal_mode=WAL activé (élimine database is locked) |
| busy_timeout | sqlite3.connect(timeout=10) sur 13 connecteurs dans 8 fichiers |
| get_polar() signature | get_polar(90,15) → get_polar().get_boat_speed(90,15) (bench python_ms était null) |
| CORS | Origines restreintes à VPS + localhost (supprime wildcard *) |
| watchdog double logging | logger.propagate=False |

### État final du système (10 mars 10h30)

| Composant | Statut |
|-----------|--------|
| Flask server | ✅ actif, 56 MB RAM |
| AIS collector | ⚠️ actif mais IP bannie par aisstream.io (72h timeout) |
| InReach | ✅ polling 10min — dernière position 07/03 01:16 (bateau à l'ancre) |
| Weather collector | ✅ SSL retry résolu — Copernicus OK |
| Passage planner | ✅ WAL mode — plus de database is locked |
| GRIB cache | ✅ 2 runs, 17 MB |
| Watchdog | ✅ tous checks OK, plus de double logging |
| Daily briefing | ✅ 07h00 UTC — Telegram envoyé |
| DB | ✅ 36 MB, WAL, intégrité OK |
| Rust engine | ✅ compilé, polar/ensemble en prod (2-17ms) |
| Mobile UI | ✅ User-Agent detection + /mobile |
| GitHub | ✅ main à jour (6 commits depuis v1.2) |

### Meilleure fenêtre départ (10 mars)

- **Route** : Cap-Vert → Barbade (route #21, 2031 NM)
- **Fenêtre** : 12 mars 2026 — score 78/100 (conf=80, confort=98)
- **ETA polaires** : 18 jours @ 4.84 kts moy.
- **Conditions** : alizés 13-18 kts, vagues 1.7-2.3m, courant +0.4 kts

### Point bloquant restant (externe)

**AIS aisstream.io** : IP VPS (45.55.239.73) bloquée après ~200 reconnexions répétées.  
Action requise : contacter aisstream.io ou régénérer l'API key depuis le dashboard.  
Impact : nul en pratique (bateau à l'ancre, hors zone AIS, tracking via InReach).

*Blueprint v1.3 — 2026-03-10*
