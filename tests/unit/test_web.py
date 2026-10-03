from coinpup_api.config import Settings
from coinpup_api.main import create_app
from fastapi.testclient import TestClient


class Probe:
    def check(self):
        pass

    def close(self):
        pass


def test_web_assets_do_not_swallow_api_routes(tmp_path):
    (tmp_path / "index.html").write_text("<h1>Coinpup</h1>", encoding="utf-8")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("export default true", encoding="utf-8")
    app = create_app(Settings(_env_file=None), Probe(), tmp_path)
    with TestClient(app) as client:
        assert "Coinpup" in client.get("/").text
        assert "Coinpup" in client.get("/login").text
        response = client.get("/assets/app.js")
        assert response.status_code == 200
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert client.get("/api/v1/nonexistent").status_code == 404
        assert client.get("/api/v1/health/live").json()["status"] == "ok"


def test_validation_response_never_echoes_password():
    with TestClient(create_app(Settings(_env_file=None), Probe())) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": {}, "password": {"value": "never-echo-this-password"}},
            headers={"Origin": "http://localhost:8000"},
        )
        assert response.status_code == 422
        assert "never-echo-this-password" not in response.text
        assert response.headers["Cache-Control"] == "no-store"
