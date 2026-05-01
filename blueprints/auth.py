"""
blueprints/auth.py — Authentification : login, logout, register, profile, fleet.
"""
import logging
import os
import random
import string

from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from blueprints.shared import get_db

logger = logging.getLogger("sailtracker_server")

bp = Blueprint("auth", __name__)

# Code d'invitation depuis .env
INVITE_CODE = os.environ.get("SAILTRACKER_INVITE_CODE", "")


@bp.route("/login", methods=["GET", "POST"])
def login_page():
    if current_user.is_authenticated:
        return redirect("/")
    if request.method == "POST":
        data = request.form
        username = data.get("username", "").strip().lower()
        password = data.get("password", "")
        conn = get_db()
        row = conn.execute(
            "SELECT id, username, email, boat_name, boat_type, is_admin, password_hash, telegram_chat_id FROM users WHERE username=? OR email=?",
            (username, username),
        ).fetchone()
        if row:
            conn.execute("UPDATE users SET last_login=datetime('now') WHERE id=?", (row["id"],))
            conn.commit()
        conn.close()
        if row and row["password_hash"] and check_password_hash(row["password_hash"], password):
            # Import local pour éviter la dépendance circulaire avec server.py
            from blueprints.shared import User
            user = User(row["id"], row["username"], row["email"], row["boat_name"], row["boat_type"], row["is_admin"], row["telegram_chat_id"])
            login_user(user, remember=True)
            return redirect(request.args.get("next") or "/")
        return render_template("login.html", error="Identifiants incorrects")
    return render_template("login.html")


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/login")


@bp.route("/register", methods=["GET", "POST"])
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
        invite_code = data.get("invite_code", "").strip()

        # Vérifier le code dans la table invite_codes (usage unique)
        conn_check = get_db()
        code_row = conn_check.execute(
            "SELECT id FROM invite_codes WHERE code=? AND used_by_user_id IS NULL",
            (invite_code,)
        ).fetchone()
        conn_check.close()
        if not code_row:
            return render_template("register.html", error="Code d'invitation invalide ou déjà utilisé.", templates=POLAR_TEMPLATES, invite_required=True)
        if len(password) < 8:
            return render_template("register.html", error="Mot de passe trop court (8 car. min)", templates=POLAR_TEMPLATES, invite_required=bool(INVITE_CODE))
        if not username or not email:
            return render_template("register.html", error="Champs requis manquants", templates=POLAR_TEMPLATES, invite_required=bool(INVITE_CODE))

        conn = get_db()
        try:
            pw_hash = generate_password_hash(password)
            cur = conn.execute(
                "INSERT INTO users (username, email, password_hash, boat_name, boat_type) VALUES (?,?,?,?,?)",
                (username, email, pw_hash, boat_name, boat_type),
            )
            user_id = cur.lastrowid
            if inreach_url:
                conn.execute("INSERT INTO inreach_configs (user_id, share_url) VALUES (?,?)", (user_id, inreach_url))
            if boat_type in POLAR_TEMPLATES:
                rows = POLAR_TEMPLATES[boat_type]["rows"]
                conn.executemany(
                    "INSERT INTO polar_matrix (twa_deg, tws_kts, speed_kts, user_id) VALUES (?,?,?,?)",
                    [(r[0], r[1], r[2], user_id) for r in rows],
                )
            # Marquer le code d'invitation comme utilisé
            conn.execute(
                "UPDATE invite_codes SET used_by_user_id=? WHERE code=?",
                (user_id, invite_code)
            )
            conn.commit()
            from blueprints.shared import User
            user = User(user_id, username, email, boat_name, boat_type, False, None)
            login_user(user, remember=True)
            return redirect("/")
        except Exception as e:
            conn.rollback()
            if "UNIQUE" in str(e):
                return render_template("register.html", error="Nom d'utilisateur ou email déjà utilisé", templates=POLAR_TEMPLATES, invite_required=True)
            return render_template("register.html", error=str(e), templates=POLAR_TEMPLATES, invite_required=True)
        finally:
            conn.close()

    return render_template("register.html", templates=POLAR_TEMPLATES, invite_required=True)


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile_page():
    from polar_templates import POLAR_TEMPLATES
    conn = get_db()
    inreach = conn.execute(
        "SELECT share_url, enabled, last_fetched FROM inreach_configs WHERE user_id=?",
        (current_user.id,),
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
            boat_type = request.form.get("boat_type", "").strip()
            reset_polars = request.form.get("reset_polars") == "1"
            if boat_type and boat_type in POLAR_TEMPLATES:
                conn.execute("UPDATE users SET boat_name=?, boat_type=? WHERE id=?", (boat_name, boat_type, current_user.id))
                if reset_polars:
                    conn.execute("DELETE FROM polar_matrix WHERE user_id=?", (current_user.id,))
                    rows = POLAR_TEMPLATES[boat_type]["rows"]
                    conn.executemany(
                        "INSERT INTO polar_matrix (user_id, twa_deg, tws_kts, speed_kts) VALUES (?,?,?,?)",
                        [(current_user.id, r[0], r[1], r[2]) for r in rows],
                    )
            else:
                conn.execute("UPDATE users SET boat_name=? WHERE id=?", (boat_name, current_user.id))
            conn.commit()
        elif action == "update_telegram":
            chat_id = request.form.get("telegram_chat_id", "").strip()
            conn.execute("UPDATE users SET telegram_chat_id=? WHERE id=?", (chat_id or None, current_user.id))
            conn.commit()
        elif action == "change_password":
            old_pw = request.form.get("old_password", "")
            new_pw = request.form.get("new_password", "")
            row = conn.execute("SELECT password_hash FROM users WHERE id=?", (current_user.id,)).fetchone()
            if row and check_password_hash(row["password_hash"], old_pw) and len(new_pw) >= 8:
                conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_pw), current_user.id))
                conn.commit()
        conn.close()
        return redirect("/profile")

    conn.close()
    return render_template("profile.html", user=current_user, inreach=inreach, templates=POLAR_TEMPLATES)


@bp.route("/fleet")
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
    users = conn.execute(
        "SELECT id, username, email, is_admin, created_at, last_login FROM users ORDER BY id"
    ).fetchall()
    conn.close()
    return render_template("fleet.html", boats=[dict(b) for b in boats],
                           users=[dict(u) for u in users], current_user=current_user)


@bp.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@login_required
def delete_user(user_id):
    """Supprime un compte utilisateur (admin uniquement, pas soi-même)."""
    if not current_user.is_admin:
        return redirect("/")
    if user_id == current_user.id:
        return redirect("/fleet")
    conn = get_db()
    conn.execute("DELETE FROM positions WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM inreach_configs WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM polar_matrix WHERE user_id=?", (user_id,))
    conn.execute("UPDATE invite_codes SET used_by_user_id=NULL WHERE used_by_user_id=?", (user_id,))
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    return redirect("/fleet")


@bp.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@login_required
def reset_user_password(user_id):
    """Réinitialise le mot de passe d'un user et retourne le nouveau en JSON."""
    if not current_user.is_admin:
        return jsonify({"error": "Non autorisé"}), 403
    import random, string
    new_pw = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
    conn = get_db()
    conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                 (generate_password_hash(new_pw), user_id))
    conn.commit()
    conn.close()
    return jsonify({"password": new_pw})



@bp.route("/admin/invite-codes")
@login_required
def invite_codes_page():
    """Page admin : liste des codes d'invitation."""
    if not current_user.is_admin:
        return redirect("/")
    conn = get_db()
    codes = conn.execute("""
        SELECT ic.id, ic.code, ic.label, ic.created_at,
               u.username as used_by_username
        FROM invite_codes ic
        LEFT JOIN users u ON u.id = ic.used_by_user_id
        ORDER BY ic.created_at DESC
    """).fetchall()
    conn.close()
    return render_template("invite_codes.html", codes=[dict(c) for c in codes])


@bp.route("/admin/invite-codes/generate", methods=["POST"])
@login_required
def generate_invite_code():
    """Génère un nouveau code d'invitation."""
    if not current_user.is_admin:
        return redirect("/")
    label = request.form.get("label", "").strip() or "Sans label"
    # Code 8 caractères : lettres majuscules + chiffres, préfixé SAIL
    suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
    code = f"SAIL{suffix}"
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO invite_codes (code, label) VALUES (?, ?)",
            (code, label)
        )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()
    return redirect("/admin/invite-codes")


@bp.route("/api/set-sam-password", methods=["POST"])
def set_sam_password():
    """One-time endpoint pour initialiser le mot de passe admin."""
    data = request.get_json() or {}
    password = data.get("password", "")
    secret = data.get("secret", "")
    if secret != "pollen_setup_2024":
        return jsonify({"error": "Non autorisé"}), 403
    if len(password) < 8:
        return jsonify({"error": "Mot de passe trop court"}), 400
    conn = get_db()
    conn.execute("UPDATE users SET password_hash=? WHERE username='sam'", (generate_password_hash(password),))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "message": "Mot de passe Sam mis à jour"})
