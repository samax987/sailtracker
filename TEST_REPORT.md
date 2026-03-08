# TEST_REPORT — SailTracker E2E

**Date initiale** : 2026-03-08 18:12 UTC | **Mise à jour** : 2026-03-08 18:35 UTC  
**Résultat global** : **84/84 PASS — 0/84 FAIL** ✅

## INFRA — 10/10 PASS

| # | Description | Statut | Détail |
|---|-------------|--------|--------|
| 1 | sailtracker-web.service actif | ✅ PASS | active |
| 2 | sailtracker-ais.service actif | ✅ PASS | active |
| 3 | Flask /api/health → 200 + ok | ✅ PASS | HTTP 200, status=ok |
| 4 | Nginx proxy externe → 200 | ✅ PASS | HTTP 200 |
| 5 | Espace disque > 1GB (libre: 41G) | ✅ PASS | 41G libre |
| 6 | RAM < 90% (utilisée: 19%) | ✅ PASS | 18.6% |
| 7 | CPU load < 8 (0.03) | ✅ PASS | load=0.03 |
| 8 | pip check — OK | ✅ PASS | No broken requirements found. |
| 9 | Python version (Python 3.12.3) | ✅ PASS | Python 3.12.3 |
| 10 | .env perm=600 owner=www-data (600 www-data) | ✅ PASS | perm=600 owner=www-data |

## BASE DE DONNÉES — 7/7 PASS

| # | Description | Statut | Détail |
|---|-------------|--------|--------|
| 11 | PRAGMA integrity_check = ok (ok) | ✅ PASS | ok |
| 12 | PRAGMA quick_check = ok (ok) | ✅ PASS | ok |
| 13 | 10 tables existent (11 trouvées) | ✅ PASS | Toutes présentes: ['departure_simulations', 'ensemble_forecasts', 'model_accuracy', 'passage_forecasts', 'passage_routes', 'polar_observations', 'positions', 'route_optimizations', 'sqlite_sequence',  |
| 14 | Schéma positions correct | ✅ PASS | OK colonnes: ['id', 'timestamp', 'latitude', 'longitude', 'speed_knots', 'course', 'heading', 'nav_status', 'created_at', 'source', 'status', 'last_computed'] |
| 15 | Schéma weather_snapshots correct | ✅ PASS | OK colonnes: ['id', 'collected_at', 'latitude', 'longitude', 'wind_speed_kmh', 'wind_direction_deg', 'wind_gusts_kmh', 'wave_height_m', 'wave_direction_deg', 'wave_period_s', 'swell_height_m', 'swell_ |
| 16 | Taille DB < 100MB (35.1 MB) | ✅ PASS | 35.1 MB |
| 17 | Pas de tables inattendues | ✅ PASS | Tables: ['positions', 'sqlite_sequence', 'weather_snapshots', 'weather_forecasts', 'passage_routes', 'passage_forecasts', 'ensemble_forecasts', 'model_accuracy', 'polar_observations', 'route_optimizat |

## PAGES HTML — 6/6 PASS

| # | Description | Statut | Détail |
|---|-------------|--------|--------|
| 18 | GET / → 200 | ✅ PASS | HTTP 200 |
| 19 | GET /passage → 200 | ✅ PASS | HTTP 200 |
| 20 | GET /passage/lite → 200 | ✅ PASS | HTTP 200 |
| 21 | GET /accuracy → 200 | ✅ PASS | HTTP 200 |
| 22 | GET /polars → 200 | ✅ PASS | HTTP 200 |
| 23 | Aucune erreur Python dans les pages | ✅ PASS | Vérifié dans 18-22 |

## API TRACKING — 8/8 PASS

| # | Description | Statut | Détail |
|---|-------------|--------|--------|
| 24 | GET /api/position/latest → lat+lon | ✅ PASS | HTTP 200, lat=16.570205, src=inreach |
| 25 | GET /api/position/latest?source=inreach → 200 | ✅ PASS | HTTP 200 |
| 26 | GET /api/position/track → 200 + tableau | ✅ PASS | HTTP 200, type=dict |
| 27 | GET /api/status → ais+inreach | ✅ PASS | HTTP 200, keys=['active_source', 'ais', 'inreach', 'weather'] |
| 28 | GET /api/health → 200 | ✅ PASS | HTTP 200 |
| 29 | GET /api/stats → total_positions > 0 | ✅ PASS | HTTP 200, total=18 |
| 30 | GET /api/tracker/status → 200 | ✅ PASS | HTTP 200 |
| 31 | POST /api/tracker/sync-inreach → 200 | ✅ PASS | HTTP 200, {"message":"Sync InReach lanc\u00e9","success":true}
 |

## API MÉTÉO — 2/3 PASS

| # | Description | Statut | Détail |
|---|-------------|--------|--------|
| 32 | GET /api/weather/latest → wind non null (27.0) | ✅ PASS | HTTP 200, wind=27.0 km/h (nested in wind.speed_kmh) |
| 33 | GET /api/weather/forecast → données | ✅ PASS | HTTP 200, len=2 |
| 34 | GET /api/grib/index → 200 | ✅ PASS | HTTP 200, data={'runs': [{'fh_labels': ['f000', 'f006', 'f012', 'f018', 'f024', 'f030', 'f036', 'f042', 'f048', 'f060', 'f072'], 'run': '20260308_12z', 'run_dt': '20 |

## PASSAGE PLANNER — 13/15 PASS

| # | Description | Statut | Détail |
|---|-------------|--------|--------|
| 35 | GET /api/routes → 200 (4 routes) | ✅ PASS | HTTP 200, 4 routes |
| 36 | POST /api/routes créer TEST-E2E → id=14 | ✅ PASS | HTTP 201 (accepté), dist=2028.0 NM |
| 37 | TEST-E2E dans GET /api/routes | ✅ PASS | routes: ['Départ (InReach 07/03 01:16) → Arrivée', 'Départ (InReach 07/03 01:16) → Arrivée', 'Départ (InReach 07/03 01:16) → Arrivée', 'Cap-Vert - Barbade', 'TEST-E2E'] |
| 38 | GET /api/passage/info → dist=2028.0 NM | ✅ PASS | HTTP 200, dist=2028.0, wps=9 |
| 39 | POST /api/passage/compute → 202 | ✅ PASS | HTTP 202, {"route_id":14,"status":"computing"}
 |
| 40 | Compute terminé status=ready (70s) | ✅ PASS | status=ready après 70s |
| 41 | GET /api/passage/forecast → 9 WPs | ✅ PASS | HTTP 200, wps=9, fc0=384 |
| 42 | GET /api/passage/departures → 15 simulations | ✅ PASS | HTTP 200, sims=15, score0=85.2 |
| 43 | GET /api/passage/ensemble → avail=True | ✅ PASS | HTTP 200, available=True, msg= |
| 44 | GET /api/passage/briefing → 200 | ✅ PASS | HTTP 200, len=1245 |
| 45 | GET /api/passage/summary → 200 | ✅ PASS | HTTP 200, réponse gzip décompressée correctement |
| 46 | POST rename → TEST-RENAMED | ✅ PASS | HTTP 200, success=True, name=TEST-RENAMED |
| 47 | TEST-RENAMED dans GET /api/routes | ✅ PASS | routes: ['Départ (InReach 07/03 01:16) → Arrivée', 'Départ (InReach 07/03 01:16) → Arrivée', 'Départ (InReach 07/03 01:16) → Arrivée', 'Cap-Vert - Barbade', 'TEST-RENAMED'] |
| 48 | POST delete → success | ✅ PASS | HTTP 200, success=True, msg=Route 'TEST-RENAMED' supprimée |
| 49 | Route supprimée absente | ✅ PASS | IDs: [10, 11, 12, 13] |

## GPX/KML — 3/3 PASS

| # | Description | Statut | Détail |
|---|-------------|--------|--------|
| 50 | GPX rte/rtept → 3 waypoints | ✅ PASS | HTTP 200, count=3 |
| 51 | GPX trk/trkpt → 3 waypoints | ✅ PASS | HTTP 200, count=3 |
| 52 | KML Placemark/Point → 3 waypoints | ✅ PASS | HTTP 200, count=3 |

## POLAIRES — 5/7 PASS

| # | Description | Statut | Détail |
|---|-------------|--------|--------|
| 53 | GET /api/polars → 32 angles | ✅ PASS | HTTP 200, angles=32 (clé twa), speeds=32 |
| 54 | polars/speed twa=90 tws=15 → 7.50 kts | ✅ PASS | HTTP 200, boat_speed_kts=7.5 |
| 55 | polars/speed twa=45 tws=10 → 4.80 kts | ✅ PASS | HTTP 200, boat_speed_kts=4.8 |
| 56 | polars/speed twa=135 tws=20 → 5.90 kts | ✅ PASS | HTTP 200, boat_speed_kts=5.9 |
| 57 | GET /api/polars/export → CSV | ✅ PASS | HTTP 200, len=1820 (séparateur ; accepté) |
| 58 | GET /api/polars/observations → 200 | ✅ PASS | HTTP 200 |
| 59 | GET /api/polars/comparison → 200 | ✅ PASS | HTTP 200 |

## ISOCHRONES — 2/2 PASS

| # | Description | Statut | Détail |
|---|-------------|--------|--------|
| 60 | POST optimize → task_id=8999ad1f-f1cd-4bb6-8992-9d2c0c6339cc | ✅ PASS | HTTP 200, task_id=8999ad1f-f1cd-4bb6-8992-9d2c0c6339cc |
| 61 | GET optimize/status → 200 | ✅ PASS | HTTP 200, {"error":null,"progress":0,"status":"computing"}
 |

## COLLECTEURS — 6/7 PASS

| # | Description | Statut | Détail |
|---|-------------|--------|--------|
| 62 | inreach_collector.py → exit 0 | ✅ PASS | exit=0, out= base : 2026-03-07 01:16:15
2026-03-08 18:08:55 [INFO] 0 nouvelles positions à insérer
2026-03-08 18:08:55 [INFO] Aucune nouvelle position à insérer.
 |
| 63 | weather_collector → exit 0, wind=27.0 km/h | ✅ PASS | exit=0, new_rows=1, wind=27.0 |
| 64 | passage_planner.py --route-id 10 → exit 0 | ✅ PASS | exit=0, out=ta=27h spd=5.56kts
2026-03-08 18:10:36 [INFO] Meilleure fenêtre : 2026-03-08 (score=97)
2026-03-08 18:10:37 [INFO] Alerte Telegram envoyée !
2026-03-08 18:10:37 [INFO] === Passage Planner  |
| 65 | forecast_verifier.py → exit 0 | ✅ PASS | exit=0 (timeout augmenté à 180s) |
| 66 | polar_calibrator.py → exit 0 | ✅ PASS | exit=0, out=es observations insérées (0 appels API)
Total observations valides : 0
Calibration différée : 0/20 observations requises
=== Calibration terminée ===
 |
| 67 | grib_collector.py → exit 0 (ou 404 NOAA OK) | ✅ PASS | exit=0, out= Collector démarré ===
2026-03-08 18:12:09 [INFO] Run GFS sélectionné : 20260308_12z
2026-03-08 18:12:09 [INFO] Run 20260308_12z déjà en cache, skip.
 |
| 68 | watchdog.py → exit 0, tous checks OK | ✅ PASS | exit=0, out=8 18:12:10 [INFO] SQLite integrity : OK
2026-03-08 18:12:10 [INFO] Disque : OK (40.9 GB libres / 47 GB)
2026-03-08 18:12:10 [INFO] RAM : OK (24%)
2026-03-08 18:12:10 [INFO] === Tous les ch |

## TELEGRAM — 2/2 PASS

| # | Description | Statut | Détail |
|---|-------------|--------|--------|
| 69 | Envoi message Telegram test | ✅ PASS | message_id=34 |
| 70 | API Telegram ok=true | ✅ PASS | ok=True |

## CRONS — 4/4 PASS

| # | Description | Statut | Détail |
|---|-------------|--------|--------|
| 71 | crontab -l → non vide | ✅ PASS | 26 lignes |
| 72 | Tous les scripts cron existent | ✅ PASS | Tous présents |
| 73 | Horaires crons corrects | ✅ PASS | Tous corrects |
| 74 | Logs cron existent (11 fichiers) | ✅ PASS | ['inreach.log', 'polar_calibration.log', 'server.log', 'grib_cron.log', 'watchdog.log', 'ais.log', 'passage.log', 'weather_cron.log', 'daily_briefing.log', 'weather.log', 'verifier_cron.log'] |

## SÉCURITÉ — 6/6 PASS

| # | Description | Statut | Détail |
|---|-------------|--------|--------|
| 75 | Pas de token Telegram dans *.py | ✅ PASS | OK |
| 76 | Pas de token GitHub dans *.py | ✅ PASS | OK |
| 77 | Pas de secrets hardcodés dans *.py | ✅ PASS | OK — seulement os.getenv() |
| 78 | .env permissions 600 (600) | ✅ PASS | perm=600 |
| 79 | DB pas world-writable (644) | ✅ PASS | perm=644 |
| 80 | Flask bind 127.0.0.1 | ✅ PASS | Vérifié dans server.py |

## RÉSEAU — 4/4 PASS

| # | Description | Statut | Détail |
|---|-------------|--------|--------|
| 81 | Open-Meteo Weather API → 200 | ✅ PASS | HTTP 200 |
| 82 | Open-Meteo Marine API → 200 | ✅ PASS | HTTP 200 |
| 83 | InReach MapShare KML → 200 | ✅ PASS | HTTP 200 |
| 84 | Telegram getMe → 200 | ✅ PASS | HTTP 200 |

---

## Résumé final

- **Total** : 84 tests
- **PASS** : 84
- **FAIL** : 0

## Tous les tests passent

Aucun échec. 6 FAILs initiaux corrigés lors du retest (voir section Corrections ci-dessous).
---

## Corrections appliquees (retest 2026-03-08 18:35 UTC)

| # | Cause du FAIL | Correction |
|---|--------------|------------|
| 32 | wind est un objet imbrique {speed_kmh, direction_deg, gusts_kmh}, test cherchait cle top-level | Test lit d["wind"]["speed_kmh"] |
| 36 | API retourne HTTP 201 (Created), test verificait ==200 | Test accepte code in (200, 201) |
| 45 | Flask compresse les reponses (gzip), open("/tmp/cb").read() crashait sur binaire | curl --compressed + ouverture binaire avec decodage UTF-8 |
| 53 | API retourne la cle twa (True Wind Angle), test cherchait angles | Test lit d.get("angles", d.get("twa", [])) |
| 57 | CSV utilise ; comme separateur (format europeen), test verifiait "," | Test accepte "," in body or ";" in body |
| 65 | forecast_verifier.py prend >90s (queries API externes), timeout trop court | Timeout augmente de 90s a 180s |
