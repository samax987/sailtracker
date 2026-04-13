"""
tests/test_routes.py — Tests des routes HTTP de SailTracker.
Utilise le client de test Flask (pas de réseau réel).
"""
import json
import os
import sys
import tempfile

import pytest

# Pointe sur le répertoire racine du projet
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture(scope="session")
def app():
    """Crée une app Flask avec une base de données temporaire."""
    # Base de données en mémoire pour les tests
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_db.close()

    os.environ.setdefault("SECRET_KEY", "test-secret-key")
    os.environ["FLASK_HOST"] = "127.0.0.1"
    os.environ["FLASK_PORT"] = "8085"

    # Patch DB_PATH avant d'importer server
    import blueprints.shared as shared
    shared.DB_PATH = type(shared.DB_PATH)(tmp_db.name)

    import server
    server.DB_PATH = type(server.DB_PATH)(tmp_db.name)

    flask_app = server.app
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    # Initialise la DB de test
    server.init_db()

    yield flask_app

    os.unlink(tmp_db.name)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def auth_client(app):
    """Client authentifié comme sam (admin)."""
    from werkzeug.security import generate_password_hash
    import sqlite3
    conn = sqlite3.connect(str(app.config.get("DB_PATH", "sailtracker.db")))
    # Met un mot de passe connu sur sam
    conn.execute(
        "UPDATE users SET password_hash=? WHERE username='sam'",
        (generate_password_hash("testpass123"),)
    )
    conn.commit()
    conn.close()

    client = app.test_client()
    client.post("/login", data={"username": "sam", "password": "testpass123"},
                follow_redirects=True)
    return client


# =============================================================================
# Pages publiques
# =============================================================================

class TestPagesPubliques:
    def test_index(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert b"html" in r.data.lower()

    def test_mobile(self, client):
        r = client.get("/mobile")
        assert r.status_code == 200

    def test_passage_page(self, client):
        r = client.get("/passage")
        assert r.status_code == 200

    def test_polars_page(self, client):
        r = client.get("/polars")
        assert r.status_code == 200

    def test_login_page(self, client):
        r = client.get("/login")
        assert r.status_code == 200
        assert b"login" in r.data.lower() or b"connexion" in r.data.lower()

    def test_static_not_found(self, client):
        r = client.get("/fichier_inexistant_xyz.html")
        assert r.status_code == 404


# =============================================================================
# API publiques (sans auth)
# =============================================================================

class TestAPIPubliques:
    def test_health(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert data.get("status") == "ok"

    def test_polars_get(self, client):
        r = client.get("/api/polars")
        assert r.status_code == 200
        data = json.loads(r.data)
        # Doit retourner un dict de polaires
        assert isinstance(data, dict)

    def test_sail_configs_active(self, client):
        r = client.get("/api/sail-configs/active")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert "active" in data


# =============================================================================
# API protégées — non authentifié → 401
# =============================================================================

class TestAPIProtegees:
    routes_protegees = [
        "/api/position/latest",
        "/api/status",
        "/api/weather/latest",
        "/api/routes",
        "/api/stats",
        "/api/sail-configs",
        "/api/at-sea",
    ]

    @pytest.mark.parametrize("url", routes_protegees)
    def test_retourne_401(self, client, url):
        r = client.get(url)
        assert r.status_code == 401, f"{url} devrait retourner 401"
        data = json.loads(r.data)
        assert "error" in data

    @pytest.mark.parametrize("url", routes_protegees)
    def test_body_json_valide(self, client, url):
        r = client.get(url)
        data = json.loads(r.data)
        assert "login_url" in data or "error" in data


# =============================================================================
# Pages protégées — non authentifié → redirect /login
# =============================================================================

class TestPagesProtegees:
    pages_protegees = ["/quart"]

    @pytest.mark.parametrize("url", pages_protegees)
    def test_redirect_login(self, client, url):
        r = client.get(url)
        assert r.status_code in (302, 301)
        assert "login" in r.headers.get("Location", "").lower()


# =============================================================================
# Auth — login / logout
# =============================================================================

class TestAuth:
    def test_login_mauvais_mdp(self, client):
        r = client.post("/login", data={"username": "sam", "password": "mauvais"},
                        follow_redirects=True)
        assert r.status_code == 200
        assert b"Identifiants incorrects" in r.data

    def test_login_utilisateur_inexistant(self, client):
        r = client.post("/login", data={"username": "inconnu", "password": "test"},
                        follow_redirects=True)
        assert r.status_code == 200
        assert b"Identifiants incorrects" in r.data

    def test_logout_redirige(self, client):
        r = client.get("/logout")
        # Redirige vers login (302) ou directement 200 si déjà déconnecté
        assert r.status_code in (302, 200)


# =============================================================================
# API sail-configs
# =============================================================================

class TestSailConfigs:
    def test_active_sans_config(self, client):
        r = client.get("/api/sail-configs/active")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert "active" in data

    def test_stats(self, client):
        r = client.get("/api/sail-configs/stats")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert "total_obs" in data
        assert "full_sail_obs" in data

    def test_quick_change_sans_auth(self, client):
        r = client.post("/api/sail-configs/quick-change",
                        json={"reef_count": 1, "genoa_pct": 80})
        # Pas de @login_required sur quick-change → doit fonctionner
        assert r.status_code in (200, 201)

    def test_quick_change_plein_voile(self, client):
        r = client.post("/api/sail-configs/quick-change",
                        json={"reef_count": 0, "genoa_pct": 100, "spinnaker": False})
        assert r.status_code in (200, 201)
        data = json.loads(r.data)
        assert data.get("description") == "Plein voile"

    def test_quick_change_ris(self, client):
        r = client.post("/api/sail-configs/quick-change",
                        json={"reef_count": 2, "genoa_pct": 80})
        assert r.status_code in (200, 201)
        data = json.loads(r.data)
        assert "2 ris" in data.get("description", "")


# =============================================================================
# API polaires
# =============================================================================

class TestPolaires:
    def test_get_polaires(self, client):
        r = client.get("/api/polars")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert isinstance(data, dict)

    def test_speed(self, client):
        r = client.get("/api/polars/speed?twa=90&tws=15")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert "boat_speed_kts" in data
        assert isinstance(data["boat_speed_kts"], (int, float))

    def test_speed_valeurs_limites(self, client):
        r = client.get("/api/polars/speed?twa=0&tws=0")
        assert r.status_code == 200

    def test_observations(self, client):
        r = client.get("/api/polars/observations")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert "observations" in data
        assert isinstance(data["observations"], list)

    def test_comparison(self, client):
        r = client.get("/api/polars/comparison")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert "comparison" in data

    def test_export(self, client):
        r = client.get("/api/polars/export")
        assert r.status_code == 200
        assert b"twa" in r.data.lower() or b"tws" in r.data.lower() or len(r.data) > 0


# =============================================================================
# API routes de passage (publiques)
# =============================================================================

class TestPassage:
    def test_routes_list_non_authentifie(self, client):
        r = client.get("/api/routes")
        assert r.status_code == 401

    def test_gpx_parse_sans_fichier(self, client):
        r = client.post("/api/gpx/parse")
        # Doit retourner une erreur propre (400 ou 401), pas un 500
        assert r.status_code in (400, 401, 422)
