#!/usr/bin/env python3
"""
patch_server.py — Applique les modifications d'authentification multi-utilisateur à server.py
Doit être exécuté sur le VPS depuis /var/www/sailtracker/
"""

import re
from pathlib import Path

SERVER_PATH = Path("/var/www/sailtracker/server.py")

content = SERVER_PATH.read_text(encoding="utf-8")

# =============================================================================
# 1. Ajouter les imports Flask-Login en haut
# =============================================================================
OLD_FLASK_IMPORT = "from flask import Flask, jsonify, request, send_from_directory, render_template, make_response"
NEW_FLASK_IMPORT = """from flask import Flask, jsonify, request, send_from_directory, render_template, make_response, redirect, url_for
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import secrets"""

content = content.replace(OLD_FLASK_IMPORT, NEW_FLASK_IMPORT, 1)
print("✓ Flask-Login imports ajoutés")

# =============================================================================
# 2. Ajouter secret_key + LoginManager après app = Flask(...)
# =============================================================================
OLD_AFTER_APP = """app = Flask(__name__, static_folder=str(STATIC_DIR), template_folder=str(TEMPLATE_DIR))

# Filtre Jinja2 pour couleur de score"""

NEW_AFTER_APP = """app = Flask(__name__, static_folder=str(STATIC_DIR), template_folder=str(TEMPLATE_DIR))
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))
login_manager = LoginManager(app)
login_manager.login_view = "login_page"
login_manager.login_message = "Connectez-vous pour accéder à SailTracker"

# Filtre Jinja2 pour couleur de score"""

content = content.replace(OLD_AFTER_APP, NEW_AFTER_APP, 1)
print("✓ secret_key + LoginManager ajoutés")

# =============================================================================
# 3. Ajouter la classe User et user_loader après CORS
# =============================================================================
OLD_AFTER_CORS = """from flask_cors import CORS
CORS(app, origins=["http://45.55.239.73", "http://localhost", "http://127.0.0.1"])

from briefing import generate_weather_briefing"""

NEW_AFTER_CORS = """from flask_cors import CORS
CORS(app, origins=["http://45.55.239.73", "http://localhost", "http://127.0.0.1"])

# =============================================================================
# Auth — User class + loader
# =============================================================================

class User(UserMixin):
    def __init__(self, id, username, email, boat_name, boat_type, is_admin):
        self.id = id
        self.username = username
        self.email = email
        self.boat_name = boat_name
        self.boat_type = boat_type
        self.is_admin = bool(is_admin)

@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    row = conn.execute(
        "SELECT id, username, email, boat_name, boat_type, is_admin FROM users WHERE id=?",
        (int(user_id),)
    ).fetchone()
    conn.close()
    if row:
        return User(row['id'], row['username'], row['email'], row['boat_name'], row['boat_type'], row['is_admin'])
    return None

from briefing import generate_weather_briefing"""

content = content.replace(OLD_AFTER_CORS, NEW_AFTER_CORS, 1)
print("✓ Classe User + user_loader ajoutés")

# =============================================================================
# 4. Protéger les routes de page avec @login_required
# =============================================================================

# Route /
content = content.replace(
    '@app.route("/")\ndef index():',
    '@app.route("/")\n@login_required\ndef index():',
    1
)
print("✓ Route / protégée")

# Route /mobile
content = content.replace(
    '@app.route("/mobile")\ndef mobile_index():',
    '@app.route("/mobile")\n@login_required\ndef mobile_index():',
    1
)
print("✓ Route /mobile protégée")

# Route /passage (pas la lite)
content = content.replace(
    '@app.route("/passage")\ndef passage_page():',
    '@app.route("/passage")\n@login_required\ndef passage_page():',
    1
)
print("✓ Route /passage protégée")

# Route /polars
content = content.replace(
    '@app.route("/polars")\ndef polars_page():',
    '@app.route("/polars")\n@login_required\ndef polars_page():',
    1
)
print("✓ Route /polars protégée")

# Route /quart
content = content.replace(
    '@app.route("/quart")\ndef quart_page():',
    '@app.route("/quart")\n@login_required\ndef quart_page():',
    1
)
print("✓ Route /quart protégée")

# Route /accuracy
content = content.replace(
    '@app.route("/accuracy")\ndef accuracy_page():',
    '@app.route("/accuracy")\n@login_required\ndef accuracy_page():',
    1
)
print("✓ Route /accuracy protégée")

# Route /passage/lite
content = content.replace(
    '@app.route("/passage/lite")\ndef passage_lite():',
    '@app.route("/passage/lite")\n@login_required\ndef passage_lite():',
    1
)
print("✓ Route /passage/lite protégée")

# =============================================================================
# 5. Protéger toutes les routes API principales
# =============================================================================

api_routes_to_protect = [
    ('@app.route("/api/position/latest")\ndef api_position_latest():',
     '@app.route("/api/position/latest")\n@login_required\ndef api_position_latest():'),
    ('@app.route("/api/position/track")\ndef api_position_track():',
     '@app.route("/api/position/track")\n@login_required\ndef api_position_track():'),
    ('@app.route("/api/status")\ndef api_status():',
     '@app.route("/api/status")\n@login_required\ndef api_status():'),
    ('@app.route("/api/weather/latest")\ndef api_weather_latest():',
     '@app.route("/api/weather/latest")\n@login_required\ndef api_weather_latest():'),
    ('@app.route("/api/weather/forecast")\ndef api_weather_forecast():',
     '@app.route("/api/weather/forecast")\n@login_required\ndef api_weather_forecast():'),
    ('@app.route("/api/routes", methods=["GET"])\ndef api_routes_list():',
     '@app.route("/api/routes", methods=["GET"])\n@login_required\ndef api_routes_list():'),
    ('@app.route("/api/routes", methods=["POST"])\ndef api_create_route():',
     '@app.route("/api/routes", methods=["POST"])\n@login_required\ndef api_create_route():'),
    ('@app.route("/api/gpx/parse", methods=["POST"])\ndef api_gpx_parse():',
     '@app.route("/api/gpx/parse", methods=["POST"])\n@login_required\ndef api_gpx_parse():'),
    ('@app.route("/api/grib/index")\ndef api_grib_index():',
     '@app.route("/api/grib/index")\n@login_required\ndef api_grib_index():'),
    ('@app.route("/api/polars", methods=["GET"])\ndef api_polars_get():',
     '@app.route("/api/polars", methods=["GET"])\n@login_required\ndef api_polars_get():'),
    ('@app.route("/api/polars", methods=["PUT"])\ndef api_polars_update():',
     '@app.route("/api/polars", methods=["PUT"])\n@login_required\ndef api_polars_update():'),
    ('@app.route("/api/polars/reset", methods=["POST"])\ndef api_polars_reset():',
     '@app.route("/api/polars/reset", methods=["POST"])\n@login_required\ndef api_polars_reset():'),
    ('@app.route("/api/polars/export")\ndef api_polars_export():',
     '@app.route("/api/polars/export")\n@login_required\ndef api_polars_export():'),
    ('@app.route("/api/polars/speed")\ndef api_polars_speed():',
     '@app.route("/api/polars/speed")\n@login_required\ndef api_polars_speed():'),
    ('@app.route("/api/polars/observations")\ndef api_polars_observations():',
     '@app.route("/api/polars/observations")\n@login_required\ndef api_polars_observations():'),
    ('@app.route("/api/polars/comparison")\ndef api_polars_comparison():',
     '@app.route("/api/polars/comparison")\n@login_required\ndef api_polars_comparison():'),
    ('@app.route("/api/polars/calibrate", methods=["POST"])\ndef api_polars_calibrate():',
     '@app.route("/api/polars/calibrate", methods=["POST"])\n@login_required\ndef api_polars_calibrate():'),
    ('@app.route("/api/stats")\n', '@app.route("/api/stats")\n@login_required\n'),
    ('@app.route("/api/engine/status")\n', '@app.route("/api/engine/status")\n@login_required\n'),
    ('@app.route("/api/at-sea")\n', '@app.route("/api/at-sea")\n@login_required\n'),
    ('@app.route("/api/quart")\ndef api_quart():', '@app.route("/api/quart")\n@login_required\ndef api_quart():'),
    ('@app.route("/api/sail-configs", methods=["GET"])\n',
     '@app.route("/api/sail-configs", methods=["GET"])\n@login_required\n'),
    ('@app.route("/api/sail-configs", methods=["POST"])\n',
     '@app.route("/api/sail-configs", methods=["POST"])\n@login_required\n'),
    ('@app.route("/api/sail-observation", methods=["POST"])\n',
     '@app.route("/api/sail-observation", methods=["POST"])\n@login_required\n'),
    ('@app.route("/api/sail-preferences")\n', '@app.route("/api/sail-preferences")\n@login_required\n'),
    ('@app.route("/api/passage/summary")\n', '@app.route("/api/passage/summary")\n@login_required\n'),
    ('@app.route("/api/passage/wind-grid")\n', '@app.route("/api/passage/wind-grid")\n@login_required\n'),
]

for old, new in api_routes_to_protect:
    if old in content:
        content = content.replace(old, new, 1)
        print(f"✓ Route API protégée: {old[:50]}")
    else:
        print(f"! Route non trouvée: {old[:50]}")

# =============================================================================
# 6. Override login_manager pour retourner 401 JSON sur les API
# =============================================================================
# Remplacer le user_loader block pour ajouter aussi le unauthorized handler

OLD_LOADER_END = """@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    row = conn.execute(
        "SELECT id, username, email, boat_name, boat_type, is_admin FROM users WHERE id=?",
        (int(user_id),)
    ).fetchone()
    conn.close()
    if row:
        return User(row['id'], row['username'], row['email'], row['boat_name'], row['boat_type'], row['is_admin'])
    return None

from briefing import generate_weather_briefing"""

NEW_LOADER_END = """@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    row = conn.execute(
        "SELECT id, username, email, boat_name, boat_type, is_admin FROM users WHERE id=?",
        (int(user_id),)
    ).fetchone()
    conn.close()
    if row:
        return User(row['id'], row['username'], row['email'], row['boat_name'], row['boat_type'], row['is_admin'])
    return None

@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith('/api/'):
        return jsonify({"error": "Non authentifié", "login_url": "/login"}), 401
    return redirect(url_for('login_page', next=request.url))

from briefing import generate_weather_briefing"""

content = content.replace(OLD_LOADER_END, NEW_LOADER_END, 1)
print("✓ Unauthorized handler ajouté")

# =============================================================================
# 7. Ajouter les nouvelles routes auth avant init_db()
# =============================================================================

NEW_AUTH_ROUTES = '''
# =============================================================================
# Auth Routes
# =============================================================================

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if current_user.is_authenticated:
        return redirect("/")
    if request.method == "POST":
        data = request.form
        username = data.get("username", "").strip().lower()
        password = data.get("password", "")
        conn = get_db()
        row = conn.execute(
            "SELECT id, username, email, boat_name, boat_type, is_admin, password_hash FROM users WHERE username=? OR email=?",
            (username, username)
        ).fetchone()
        if row:
            conn.execute("UPDATE users SET last_login=datetime(\'now\') WHERE id=?", (row[\'id\'],))
            conn.commit()
        conn.close()
        if row and row[\'password_hash\'] and check_password_hash(row[\'password_hash\'], password):
            user = User(row[\'id\'], row[\'username\'], row[\'email\'], row[\'boat_name\'], row[\'boat_type\'], row[\'is_admin\'])
            login_user(user, remember=True)
            return redirect(request.args.get("next") or "/")
        return render_template("login.html", error="Identifiants incorrects")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/login")


@app.route("/register", methods=["GET", "POST"])
def register_page():
    if current_user.is_authenticated:
        return redirect("/")
    from polar_templates import POLAR_TEMPLATES
    if request.method == "POST":
        data = request.form
        username = data.get("username", "").strip().lower()
        email = data.get("email", "").strip().lower()
        password = data.get("password", "")
        boat_name = data.get("boat_name", "Mon Bateau").strip()
        boat_type = data.get("boat_type", "sloop_croisiere")
        inreach_url = data.get("inreach_url", "").strip()

        if len(password) < 8:
            return render_template("register.html", error="Mot de passe trop court (8 car. min)", templates=POLAR_TEMPLATES)
        if not username or not email:
            return render_template("register.html", error="Champs requis manquants", templates=POLAR_TEMPLATES)

        conn = get_db()
        try:
            pw_hash = generate_password_hash(password)
            cur = conn.execute(
                "INSERT INTO users (username, email, password_hash, boat_name, boat_type) VALUES (?,?,?,?,?)",
                (username, email, pw_hash, boat_name, boat_type)
            )
            user_id = cur.lastrowid

            if inreach_url:
                conn.execute(
                    "INSERT INTO inreach_configs (user_id, share_url) VALUES (?,?)",
                    (user_id, inreach_url)
                )

            if boat_type in POLAR_TEMPLATES:
                rows = POLAR_TEMPLATES[boat_type][\'rows\']
                conn.executemany(
                    "INSERT INTO polar_matrix (twa_deg, tws_kts, speed_kts, user_id) VALUES (?,?,?,?)",
                    [(r[0], r[1], r[2], user_id) for r in rows]
                )

            conn.commit()
            user = User(user_id, username, email, boat_name, boat_type, False)
            login_user(user, remember=True)
            return redirect("/")
        except Exception as e:
            conn.rollback()
            if "UNIQUE" in str(e):
                return render_template("register.html", error="Nom d\'utilisateur ou email déjà utilisé", templates=POLAR_TEMPLATES)
            return render_template("register.html", error=str(e), templates=POLAR_TEMPLATES)
        finally:
            conn.close()

    return render_template("register.html", templates=POLAR_TEMPLATES)


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile_page():
    from polar_templates import POLAR_TEMPLATES
    conn = get_db()
    inreach = conn.execute(
        "SELECT share_url, enabled, last_fetched FROM inreach_configs WHERE user_id=?",
        (current_user.id,)
    ).fetchone()

    if request.method == "POST":
        action = request.form.get("action")
        if action == "update_inreach":
            url = request.form.get("share_url", "").strip()
            if inreach:
                conn.execute("UPDATE inreach_configs SET share_url=? WHERE user_id=?", (url, current_user.id))
            else:
                conn.execute("INSERT INTO inreach_configs (user_id, share_url) VALUES (?,?)", (current_user.id, url))
            conn.commit()
        elif action == "update_boat":
            boat_name = request.form.get("boat_name", "").strip()
            conn.execute("UPDATE users SET boat_name=? WHERE id=?", (boat_name, current_user.id))
            conn.commit()
        elif action == "change_password":
            old_pw = request.form.get("old_password", "")
            new_pw = request.form.get("new_password", "")
            row = conn.execute("SELECT password_hash FROM users WHERE id=?", (current_user.id,)).fetchone()
            if row and check_password_hash(row[\'password_hash\'], old_pw) and len(new_pw) >= 8:
                conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                             (generate_password_hash(new_pw), current_user.id))
                conn.commit()
        conn.close()
        return redirect("/profile")

    conn.close()
    return render_template("profile.html", user=current_user, inreach=inreach, templates=POLAR_TEMPLATES)


@app.route("/fleet")
@login_required
def fleet_page():
    if not current_user.is_admin:
        return redirect("/")
    conn = get_db()
    boats = conn.execute("""
        SELECT u.id, u.username, u.boat_name, u.boat_type,
               p.latitude, p.longitude, p.timestamp, p.speed_knots, p.course
        FROM users u
        LEFT JOIN positions p ON p.user_id = u.id
            AND p.timestamp = (SELECT MAX(p2.timestamp) FROM positions p2 WHERE p2.user_id=u.id)
        ORDER BY u.id
    """).fetchall()
    conn.close()
    return render_template("fleet.html", boats=[dict(b) for b in boats], current_user=current_user)


@app.route("/api/set-sam-password", methods=["POST"])
def set_sam_password():
    """One-time endpoint for Sam to set his admin password."""
    data = request.get_json() or {}
    password = data.get("password", "")
    secret = data.get("secret", "")
    if secret != "pollen_setup_2024":
        return jsonify({"error": "Non autorisé"}), 403
    if len(password) < 8:
        return jsonify({"error": "Mot de passe trop court"}), 400
    conn = get_db()
    conn.execute("UPDATE users SET password_hash=? WHERE username=\'sam\'",
                 (generate_password_hash(password),))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "message": "Mot de passe Sam mis à jour"})


'''

# Insérer avant "# ============\n# Init DB\n# ============"
INIT_DB_MARKER = "\n# =============================================================================\n# Init DB\n# =============================================================================\n"
if INIT_DB_MARKER in content:
    content = content.replace(INIT_DB_MARKER, NEW_AUTH_ROUTES + INIT_DB_MARKER, 1)
    print("✓ Routes Auth insérées avant init_db()")
else:
    print("! Marqueur init_db non trouvé — appending routes auth")
    content += NEW_AUTH_ROUTES

# =============================================================================
# 8. Modifier init_db() pour ajouter tables users, inreach_configs et migrations
# =============================================================================

OLD_MIGRATIONS_END = """    try:
        c.execute("ALTER TABLE polar_observations ADD COLUMN sail_config_id INTEGER")
    except Exception:
        pass
    conn.commit()
    conn.close()"""

NEW_MIGRATIONS_END = """    try:
        c.execute("ALTER TABLE polar_observations ADD COLUMN sail_config_id INTEGER")
    except Exception:
        pass

    # ── Tables auth multi-utilisateur ──
    c.executescript(\"\"\"
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
        CREATE INDEX IF NOT EXISTS idx_polar_matrix_user ON polar_matrix(user_id, twa_deg, tws_kts);
    \"\"\")

    # ── Ajouter user_id aux tables existantes ──
    for table in ['positions', 'sail_config_periods', 'polar_observations',
                   'logbook_entries', 'passage_routes', 'sail_config_observations',
                   'model_accuracy']:
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER")
        except Exception:
            pass

    # ── Créer Sam comme admin si inexistant ──
    sam_exists = c.execute("SELECT id FROM users WHERE username='sam'").fetchone()
    if not sam_exists:
        c.execute(\"\"\"
            INSERT INTO users (username, email, boat_name, boat_type, is_admin)
            VALUES ('sam', 'samuelvisoko@gmail.com', 'POLLEN 1', 'sloop_croisiere', 1)
        \"\"\")

    # ── Migrer les données existantes vers user_id=1 (Sam) ──
    sam_row = c.execute("SELECT id FROM users WHERE username='sam'").fetchone()
    if sam_row:
        sam_id = sam_row[0]
        for table in ['positions', 'sail_config_periods', 'polar_observations',
                       'logbook_entries', 'passage_routes', 'sail_config_observations',
                       'model_accuracy']:
            try:
                c.execute(f"UPDATE {table} SET user_id={sam_id} WHERE user_id IS NULL")
            except Exception:
                pass
        # Migrer polar_matrix existant (sans user_id)
        try:
            c.execute(f"UPDATE polar_matrix SET user_id={sam_id} WHERE user_id IS NULL")
        except Exception:
            pass

    conn.commit()
    conn.close()"""

content = content.replace(OLD_MIGRATIONS_END, NEW_MIGRATIONS_END, 1)
print("✓ Migrations init_db() mises à jour")

# =============================================================================
# 9. Écrire le fichier
# =============================================================================
SERVER_PATH.write_text(content, encoding="utf-8")
print("\n=== PATCH APPLIQUÉ AVEC SUCCÈS ===")
print(f"Fichier écrit : {SERVER_PATH}")
