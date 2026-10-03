import logging

import pytest
from coinpup_api.config import Settings
from coinpup_api.main import create_app
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy.exc import OperationalError


class Probe:
    def __init__(self, failing=False):
        self.failing = failing
        self.calls = 0
        self.closed = False

    def check(self):
        self.calls += 1
        if self.failing:
            raise OperationalError("SELECT 1", {}, Exception("private-password-and-host"))

    def close(self):
        self.closed = True


def settings(**overrides):
    return Settings(_env_file=None, environment="test", **overrides)


def test_liveness_does_not_depend_on_database():
    probe = Probe(failing=True)
    with TestClient(create_app(settings(), probe)) as client:
        response = client.get("/api/v1/health/live")
        assert response.status_code == 200
        assert probe.calls == 0
    assert probe.closed


def test_database_outage_is_reported_without_exposing_connection_details(caplog):
    probe = Probe(failing=True)
    with TestClient(create_app(settings(), probe)) as client:
        with caplog.at_level(logging.WARNING, logger="coinpup"):
            response = client.get("/api/v1/health/ready")
        assert response.status_code == 503
        assert response.json() == {"service": "coinpup-api", "status": "unavailable"}
        assert "private-password" not in response.text + caplog.text
        probe.failing = False
        assert client.get("/api/v1/health/ready").status_code == 200


def test_production_disables_api_explorer():
    config = Settings(_env_file=None, environment="production")
    with TestClient(create_app(config, Probe())) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/api/v1/health/live").status_code == 200


def test_configuration_does_not_silently_fall_back_to_another_database():
    with pytest.raises(ValidationError):
        settings(database_url=SecretStr("sqlite:///local.db"))


def test_database_password_is_masked_in_settings_representation():
    config = settings(database_url=SecretStr("postgresql+psycopg://user:secret-value@db/coinpup"))
    assert "secret-value" not in repr(config)


@pytest.mark.parametrize(
    "url", ["postgres://user:secret-value@db/coinpup", "malformed-secret-value"]
)
def test_invalid_database_configuration_does_not_expose_raw_input(url):
    with pytest.raises(ValidationError) as error:
        settings(database_url=url)
    assert "secret-value" not in str(error.value)
