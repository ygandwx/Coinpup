"""Real session + HTTP + persistent structure acceptance on disposable PostgreSQL."""

import os
from datetime import UTC, datetime, timedelta

import pytest
from coinpup_api.config import Settings
from coinpup_api.main import create_app
from coinpup_api.models import AuthSession
from coinpup_api.security import csrf_token_for, token_digest
from fastapi.testclient import TestClient
from sqlalchemy import func, update
from sqlalchemy.orm import Session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COINPUP_RUN_DB_TESTS") != "1",
        reason="Requires an explicitly configured disposable PostgreSQL database",
    ),
]
ORIGIN = "http://localhost:8000"


def test_real_session_structure_workflow_and_versions(structure_database):
    engine, owner_id = structure_database

    class Probe:
        def __init__(self):
            self.engine = engine

        def check(self):
            pass

        def close(self):
            pass  # Fixture owns the engine lifecycle.

    token = "fictional-ledger-http-session"
    with Session(engine) as session, session.begin():
        session.add(
            AuthSession(
                token_hash=token_digest(token),
                administrator_id=owner_id,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    app = create_app(Settings(_env_file=None, environment="test"), Probe())
    with TestClient(app) as client:
        assert client.get("/api/v1/entities").status_code == 401
        client.cookies.set("coinpup_session", token)
        headers = {"origin": ORIGIN, "x-csrf-token": csrf_token_for(token)}
        client.headers.update(headers)
        personal = client.post(
            "/api/v1/entities",
            json={
                "kind": "personal",
                "name": "Fictional personal",
                "base_asset_id": "USD",
                "template_key": "business_default",
            },
        )
        assert personal.status_code == 201, personal.text
        p = personal.json()
        company = client.post(
            "/api/v1/entities",
            json={
                "kind": "company",
                "name": "Fictional company",
                "base_asset_id": "USD",
                "country_code": "US",
                "region_code": "NM",
                "company_type": "llc",
                "template_key": "business_default",
            },
        )
        assert company.status_code == 201, company.text
        c = company.json()
        left = f"/api/v1/ledgers/{p['ledger']['id']}"
        right = f"/api/v1/ledgers/{c['ledger']['id']}"
        assert client.get(left).json()["entity_id"] == p["id"]
        account = client.post(
            left + "/accounts",
            json={
                "kind": "wise",
                "name": "Fictional wallet",
                "asset_ids": ["USD", "EUR"],
            },
        )
        assert account.status_code == 201, account.text
        a = account.json()
        assert a["asset_ids"] == ["EUR", "USD"]
        assert client.get(right + "/accounts").json() == []
        assert (
            client.patch(
                right + f"/accounts/{a['id']}",
                json={
                    "expected_version": 1,
                    "name": "Cross-ledger forbidden",
                },
            ).status_code
            == 404
        )
        invalid = client.post(
            left + "/accounts",
            json={
                "kind": "cash",
                "name": "Must roll back",
                "asset_ids": ["USD", "UNKNOWN"],
            },
        )
        assert invalid.status_code == 404
        assert len(client.get(left + "/accounts").json()) == 1

        categories = client.get(left + "/categories").json()
        other = client.get(right + "/categories").json()
        target = next(item for item in categories if item["kind"] == "expense")
        matching = next(item for item in other if item["template_key"] == target["template_key"])
        updated = client.patch(
            left + f"/categories/{target['id']}",
            json={
                "expected_version": 1,
                "name": "独立修改",
            },
        )
        assert updated.status_code == 200 and updated.json()["version"] == 2
        assert (
            next(
                item
                for item in client.get(right + "/categories").json()
                if item["id"] == matching["id"]
            )["name"]
            == matching["name"]
        )
        stale = client.patch(
            left + f"/categories/{target['id']}",
            json={
                "expected_version": 1,
                "name": "Stale update",
            },
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "version_conflict"
        assert (
            client.post(
                right + "/categories",
                json={
                    "name": "Cross-ledger child",
                    "kind": "expense",
                    "parent_id": target["id"],
                },
            ).status_code
            == 404
        )

        archived = client.patch(
            f"/api/v1/entities/{p['id']}",
            json={
                "expected_version": 1,
                "archived": True,
            },
        )
        assert archived.status_code == 200
        assert len(client.get("/api/v1/entities").json()) == 1
        assert len(client.get("/api/v1/entities?include_archived=true").json()) == 2
        blocked = client.post(
            left + "/accounts",
            json={
                "kind": "cash",
                "name": "Archived ledger",
                "asset_ids": ["USD"],
            },
        )
        assert blocked.status_code == 409
        assert len(client.get(left + "/accounts").json()) == 1
        restored = client.patch(
            f"/api/v1/entities/{p['id']}",
            json={
                "expected_version": 2,
                "archived": False,
            },
        )
        assert restored.status_code == 200 and restored.json()["version"] == 3
        with engine.begin() as connection:
            connection.execute(update(AuthSession).values(revoked_at=func.now()))
        assert client.get("/api/v1/entities").status_code == 401
        assert (
            client.patch(
                f"/api/v1/entities/{p['id']}",
                json={
                    "expected_version": 3,
                    "name": "Revoked session",
                },
            ).status_code
            == 401
        )
