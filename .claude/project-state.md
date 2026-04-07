# Project State — SailTracker

## Last Updated
2026-04-06T12:00:00Z

## Current Task
Passage planner à intégrer : multi-modèle météo, ensembles ECMWF/GFS, departure planning.

## Status
IN_PROGRESS

## Context
SailTracker est un outil de weather routing et passage planning personnel, construit pour la
traversée atlantique de Sam (Mindelo → Sint Maarten, mars 2026). Le système tourne sur le VPS
DigitalOcean port 8085. Il inclut : collecteur AIS via aisstream.io websocket, collecteur météo
(cron 3h), intégration courants océaniques RTOFS, moteur isochrone en Rust (sailtracker-engine),
calibration automatique des polaires depuis les données GPS InReach, interface cockpit PWA (port 8086),
et briefing quotidien Telegram.

La traversée est terminée (arrivée Sint Maarten avril 2026). Prochaine évolution : ajouter un
passage planner avec données multi-modèles et ensembles pour les navigations caribéennes.

## Files Modified This Session
- Aucun (état initial)

## Next Steps
1. Ajouter les tables SQLite pour le passage planner (passage_routes, ensemble_forecasts, model_comparison, departure_simulations)
2. Créer passage_planner.py — moteur de calcul multi-modèle
3. Créer forecast_verifier.py — cron quotidien prévision vs réalité
4. Ajouter la vue "Passage Planner" sur l'interface Leaflet

## Blockers / Open Questions
- Accès API ECMWF ensemble (51 membres) : vérifier quotas gratuits
- Calibration polaires : données InReach disponibles pour la traversée atlantique

## Key Decisions Made
- Rust pour le moteur isochrone (performance)
- SQLite pour tout le stockage (pas de Postgres)
- PWA cockpit sur port séparé 8086 (utilisable offline)
- Briefing Telegram quotidien automatique via cron
- Open-Meteo comme source météo principale (gratuit, fiable)

## Architecture Notes
- app.py : serveur Flask principal (port 8085)
- sailtracker-engine/ : moteur isochrone Rust
- collectors/ : AIS websocket + météo cron
- static/ : interface Leaflet + cockpit PWA
- sailtracker.db : SQLite (positions, weather_snapshots, weather_forecasts)
- Garmin InReach Mini pour tracking satellite
- Quark-Elec A027 AIS receiver connecté via WiFi ad-hoc à Navionics

## Session History
### 2026-03 — Traversée atlantique
- Système utilisé en conditions réelles pendant la traversée
- Moteur isochrone Rust fonctionnel
- Calibration polaires automatique depuis données InReach
- Briefing Telegram quotidien opérationnel
