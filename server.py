#!/usr/bin/env python3
"""
server.py — Serveur Flask SailTracker
Point d'entrée : init Flask, auth, blueprints, init_db.
"""

import logging
import logging.handlers
import os
import sqlite3
from pathlib import Path

import secrets
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, url_for
from flask_cors import CORS
from flask_login import LoginManager

from blueprints.shared import User, get_db, DB_PATH, STATIC_DIR

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

TEMPLATE_DIR = BASE_DIR / "templates"
FLASK_HOST = os.getenv("FLASK_HOST", "127.0.0.1")
FLASK_PORT = int(os.getenv("FLASK_PORT", "8085"))


# =============================================================================
# Logging
# =============================================================================

def setup_logging():
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    logger = logging.getLogger("sailtracker_server")
    logger.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "server.log", maxBytes=10*1024*1024, backupCount=5, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)
    return logger

logger = setup_logging()

# =============================================================================
# App Flask
# =============================================================================

app = Flask(__name__, static_folder=str(STATIC_DIR), template_folder=str(TEMPLATE_DIR))
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))

login_manager = LoginManager(app)
login_manager.login_view = "auth.login_page"
login_manager.login_message = "Connectez-vous pour accéder à SailTracker"

CORS(app, origins=["http://45.55.239.73", "http://localhost", "http://127.0.0.1"])

# Filtres Jinja2 pour les scores météo
@app.template_filter('score_color')
def score_color_filter(score):
    if score >= 70: return '#3fb950'
    if score >= 50: return '#d29922'
    return '#f85149'

@app.template_filter('score_label')
def score_label_filter(score):
    if score >= 70: return 'GO'
    if score >= 50: return 'MOYEN'
    return 'MAUVAIS'


# =============================================================================
# Auth — User loader + unauthorized handler
# =============================================================================

@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    row = conn.execute(
        "SELECT id, username, email, boat_name, boat_type, is_admin, telegram_chat_id FROM users WHERE id=?",
        (int(user_id),)
    ).fetchone()
    conn.close()
    if row:
        return User(row['id'], row['username'], row['email'], row['boat_name'],
                    row['boat_type'], row['is_admin'], row['telegram_chat_id'])
    return None


@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith('/api/'):
        return jsonify({"error": "Non authentifié", "login_url": "/login"}), 401
    return redirect(url_for('auth.login_page', next=request.url))


# =============================================================================
# Blueprints
# =============================================================================

from blueprints.system import bp as system_bp
from blueprints.auth import bp as auth_bp
from blueprints.weather import bp as weather_bp
from blueprints.tracking import bp as tracking_bp
from blueprints.logbook import bp as logbook_bp
from blueprints.passage import bp as passage_bp
from blueprints.sailing import bp as sailing_bp
from blueprints.analysis import bp as analysis_bp
from blueprints.web import bp as web_bp

app.register_blueprint(system_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(weather_bp)
app.register_blueprint(tracking_bp)
app.register_blueprint(logbook_bp)
app.register_blueprint(passage_bp)
app.register_blueprint(sailing_bp)
app.register_blueprint(analysis_bp)
app.register_blueprint(web_bp)


# =============================================================================
# Init DB — création des tables et migrations
# =============================================================================

def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
            speed_knots REAL, course REAL, heading REAL, nav_status TEXT,
            source TEXT DEFAULT 'ais', created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_positions_timestamp ON positions(timestamp DESC);

        CREATE TABLE IF NOT EXISTS weather_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
            wind_speed_kmh REAL, wind_direction_deg REAL, wind_gusts_kmh REAL,
            wave_height_m REAL, wave_direction_deg REAL, wave_period_s REAL,
            swell_height_m REAL, swell_direction_deg REAL, swell_period_s REAL,
            current_speed_knots REAL, current_direction_deg REAL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_weather_collected ON weather_snapshots(collected_at DESC);

        CREATE TABLE IF NOT EXISTS weather_forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at TEXT NOT NULL, forecast_time TEXT NOT NULL, data_type TEXT NOT NULL,
            value1 REAL, value2 REAL, value3 REAL, created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_forecast_time ON weather_forecasts(forecast_time);

        CREATE TABLE IF NOT EXISTS passage_routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            waypoints TEXT NOT NULL, boat_speed_avg_knots REAL DEFAULT 6.0,
            max_wind_knots REAL DEFAULT 30, max_wave_m REAL DEFAULT 3.0,
            max_swell_m REAL DEFAULT 3.5, status TEXT DEFAULT 'ready',
            last_computed TEXT, created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS passage_forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_id INTEGER NOT NULL, collected_at TEXT NOT NULL,
            waypoint_index INTEGER NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
            forecast_time TEXT NOT NULL, model TEXT NOT NULL,
            wind_speed_knots REAL, wind_direction_deg REAL, wind_gusts_knots REAL,
            wave_height_m REAL, wave_direction_deg REAL, wave_period_s REAL,
            swell_height_m REAL, swell_direction_deg REAL, swell_period_s REAL,
            FOREIGN KEY (route_id) REFERENCES passage_routes(id)
        );
        CREATE INDEX IF NOT EXISTS idx_passage_fc ON passage_forecasts(route_id,collected_at,model);

        CREATE TABLE IF NOT EXISTS departure_simulations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_id INTEGER NOT NULL, computed_at TEXT NOT NULL,
            departure_date TEXT NOT NULL, confidence_score REAL, comfort_score REAL,
            overall_score REAL, summary TEXT, alerts TEXT,
            FOREIGN KEY (route_id) REFERENCES passage_routes(id)
        );
        CREATE INDEX IF NOT EXISTS idx_dep_sim ON departure_simulations(route_id,computed_at);

        CREATE TABLE IF NOT EXISTS ensemble_forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at TEXT NOT NULL,
            route_id INTEGER NOT NULL,
            waypoint_index INTEGER NOT NULL,
            forecast_time TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT 'ecmwf_ens',
            member_id INTEGER NOT NULL,
            wind_speed_knots REAL,
            wind_direction_deg REAL,
            FOREIGN KEY (route_id) REFERENCES passage_routes(id)
        );
        CREATE INDEX IF NOT EXISTS idx_ens_query ON ensemble_forecasts(route_id, waypoint_index, collected_at);

        CREATE TABLE IF NOT EXISTS model_accuracy (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            model TEXT NOT NULL,
            zone TEXT NOT NULL,
            forecast_hour INTEGER NOT NULL,
            wind_speed_error_avg REAL,
            wind_dir_error_avg REAL,
            sample_count INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(date, model, zone, forecast_hour)
        );
        CREATE INDEX IF NOT EXISTS idx_acc_lookup ON model_accuracy(model, zone, forecast_hour, date);

        CREATE TABLE IF NOT EXISTS polar_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            latitude REAL, longitude REAL,
            sog_kts REAL, cog_deg REAL,
            tws_kts REAL, twd_deg REAL, twa_deg REAL,
            current_speed_kts REAL, current_dir_deg REAL,
            stw_kts REAL,
            is_valid INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_polar_obs ON polar_observations(timestamp DESC);

        CREATE TABLE IF NOT EXISTS route_optimizations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_id INTEGER NOT NULL,
            computed_at TEXT NOT NULL,
            departure TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_route_opt ON route_optimizations(route_id, computed_at DESC);

        CREATE TABLE IF NOT EXISTS logbook_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            entry_type TEXT NOT NULL DEFAULT 'note',
            text TEXT,
            latitude REAL,
            longitude REAL,
            wind_speed_kts REAL,
            wind_dir_deg REAL,
            sog_kts REAL,
            sea_state TEXT,
            sail_config TEXT,
            created_by TEXT DEFAULT 'manual',
            FOREIGN KEY (route_id) REFERENCES passage_routes(id)
        );
        CREATE INDEX IF NOT EXISTS idx_logbook ON logbook_entries(route_id, timestamp DESC);

        CREATE TABLE IF NOT EXISTS sail_config_periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_start TEXT NOT NULL,
            timestamp_end   TEXT,
            reef_count      INTEGER NOT NULL DEFAULT 0,
            genoa_pct       INTEGER NOT NULL DEFAULT 100,
            spinnaker       INTEGER NOT NULL DEFAULT 0,
            description     TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_sail_cfg ON sail_config_periods(timestamp_start);

        CREATE TABLE IF NOT EXISTS sail_config_observations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT NOT NULL DEFAULT (datetime('now')),
            tws         REAL,
            twa         REAL,
            rec_reef    INTEGER,
            rec_genoa   INTEGER,
            rec_spi     INTEGER DEFAULT 0,
            actual_reef INTEGER NOT NULL,
            actual_genoa INTEGER NOT NULL,
            actual_spi  INTEGER DEFAULT 0,
            sog         REAL
        );
        CREATE INDEX IF NOT EXISTS idx_sailobs_ts ON sail_config_observations(timestamp DESC);
    """)
    # Migrations
    for col, definition in [("status", "TEXT DEFAULT 'ready'"), ("last_computed", "TEXT"), ("source", "TEXT DEFAULT 'ais'")]:
        try: c.execute(f"ALTER TABLE passage_routes ADD COLUMN {col} {definition}")
        except Exception: pass
    for col, definition in [("current_speed_knots", "REAL"), ("current_direction_deg", "REAL")]:
        try: c.execute(f"ALTER TABLE passage_forecasts ADD COLUMN {col} {definition}")
        except Exception: pass
    for col, definition in [
        ("phase", "TEXT DEFAULT 'planning'"), ("actual_departure", "TEXT"),
        ("actual_arrival", "TEXT"), ("departure_port", "TEXT"),
        ("arrival_port", "TEXT"), ("notes", "TEXT"),
    ]:
        try: c.execute(f"ALTER TABLE passage_routes ADD COLUMN {col} {definition}")
        except Exception: pass
    try: c.execute("ALTER TABLE polar_observations ADD COLUMN sail_config_id INTEGER")
    except Exception: pass

    # Tables auth multi-utilisateur
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT,
            boat_name TEXT NOT NULL DEFAULT 'Mon Bateau',
            boat_type TEXT DEFAULT 'sloop_croisiere',
            is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            last_login TEXT
        );
        CREATE TABLE IF NOT EXISTS inreach_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            share_url TEXT NOT NULL,
            feed_password TEXT,
            enabled INTEGER DEFAULT 1,
            last_fetched TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS polar_matrix (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            twa_deg REAL NOT NULL,
            tws_kts REAL NOT NULL,
            speed_kts REAL NOT NULL,
            user_id INTEGER
        );
    """)

    # Ajouter user_id aux tables existantes
    for table in ['positions', 'sail_config_periods', 'polar_observations',
                   'logbook_entries', 'passage_routes', 'sail_config_observations',
                   'model_accuracy', 'polar_matrix']:
        try: c.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER")
        except Exception: pass
    try: c.execute("CREATE INDEX IF NOT EXISTS idx_polar_matrix_user ON polar_matrix(user_id, twa_deg, tws_kts)")
    except Exception: pass

    # Créer Sam comme admin si inexistant
    if not c.execute("SELECT id FROM users WHERE username='sam'").fetchone():
        c.execute("""
            INSERT INTO users (username, email, boat_name, boat_type, is_admin)
            VALUES ('sam', 'samuelvisoko@gmail.com', 'POLLEN 1', 'sloop_croisiere', 1)
        """)

    # Migrer les données existantes vers user_id=1 (Sam)
    sam_row = c.execute("SELECT id FROM users WHERE username='sam'").fetchone()
    if sam_row:
        sam_id = sam_row[0]
        for table in ['positions', 'sail_config_periods', 'polar_observations',
                       'logbook_entries', 'passage_routes', 'sail_config_observations',
                       'model_accuracy', 'polar_matrix']:
            try: c.execute(f"UPDATE {table} SET user_id={sam_id} WHERE user_id IS NULL")
            except Exception: pass

    conn.commit()
    conn.close()


if __name__ == "__main__":
    logger.info("=== SailTracker Web Server démarré sur %s:%d ===", FLASK_HOST, FLASK_PORT)
    init_db()
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False)
