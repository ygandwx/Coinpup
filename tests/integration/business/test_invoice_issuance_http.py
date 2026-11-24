"""Real browser session, original raw intent, scope and no-store boundaries."""

from uuid import uuid4

import pytest
from coinpup_api.business.invoice_reservation_command import InvoiceReservationCommand
from coinpup_api.ledger.schemas import EntityCreate

from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_reservation_command import args
from tests.integration.business.test_invoice_snapshot_binding import company as company
from tests.integration.business.test_invoice_snapshot_binding import reviewed
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.fixture
def issuance_http_environment(monkeypatch, tmp_path):
    # These operator-selected paths must never come from a request body.
    monkeypatch.setenv("COINPUP_FILES_DIRECTORY", str(tmp_path / "fictional-private-files"))
    monkeypatch.setenv("COINPUP_INVOICE_FONT_DIR", str(tmp_path / "missing-fonts"))


@pytest.fixture
def http_case(issuance_http_environment, authenticated_client, company):
    client, engine, owner = authenticated_client
    assert engine is company["engine"] and owner == company["owner"]
    row = reviewed(company, uppercase=True)
    return company, client, row


def path(ledger):
    return f"/api/v1/ledgers/{ledger}/invoice-issuances"


def post(client, url, row, **changes):
    request = {
        "content": row["request_json"].encode(),
        "headers": {"Idempotency-Key": row["idempotency_key"], "Content-Type": "application/json"},
    } | changes
    return client.post(url, **request)


@pytest.mark.parametrize(
    "fault",
    ["session", "csrf", "origin", "absent-key", "invalid-key", "client-number", "font-path"],
)
def test_invalid_or_unauthorized_write_does_not_allocate_a_number(http_case, fault):
    s, client, row = http_case
    before = snapshot(s)
    headers = {"content-type": "application/json", "idempotency-key": row["idempotency_key"]}
    body = row["request_json"]
    if fault == "session":
        client.cookies.clear()
    elif fault == "csrf":
        client.headers.pop("x-csrf-token")
    elif fault == "origin":
        client.headers["origin"] = "https://fictional.example.invalid"
    elif fault == "absent-key":
        headers.pop("idempotency-key")
    elif fault == "invalid-key":
        headers["idempotency-key"] = "fictional invalid key"
    else:
        import json

        body = json.dumps(
            json.loads(body)
            | (
                {"number": "invented"}
                if fault == "client-number"
                else {"font_directory": "/fictional/path"}
            )
        )
    response = post(client, path(s["ledger"]), row, content=body.encode(), headers=headers)
    assert response.status_code == ({"session": 401, "csrf": 403, "origin": 403}.get(fault, 422))
    assert response.headers["cache-control"] == "no-store" and snapshot(s) == before


def test_failed_render_keeps_exact_pending_owner_body_and_does_not_expose_or_rebind(http_case):
    s, client, row = http_case
    url = path(s["ledger"])
    resource = url + "/" + str(row["id"])
    assert client.get(resource).status_code == 404
    failed = post(client, url, row)
    assert failed.status_code == 503 and failed.headers["cache-control"] == "no-store"
    assert set(failed.json()["detail"]) == {"code", "message"}
    assert "missing-fonts" not in failed.text
    observed = client.get(resource)
    assert observed.status_code == 200 and observed.headers["cache-control"] == "no-store"
    pending = observed.json()
    assert (
        pending["state"] == "pending"
        and pending["artifacts"] == []
        and pending["number"] == row["number"]
    )
    assert pending == InvoiceReservationCommand(s["engine"]).reserve(**args(s, row)).model_dump(
        mode="json"
    )
    foreign = (
        s["structure"]
        .create_entity(
            s["owner"],
            EntityCreate(
                kind="company",
                name="Fictional separate issuer",
                country_code="HK",
                company_type="private_limited",
                base_asset_id="USD",
            ),
        )
        .ledger.id
    )
    before = snapshot(s)
    assert client.get(path(foreign) + "/" + str(row["id"])).status_code == 404
    assert client.get(url + "/" + str(uuid4())).status_code == 404
    different = post(
        client,
        url,
        row,
        content=row["request_json"]
        .replace(str(row["id"]).upper(), str(row["id"]).lower())
        .encode(),
    )
    assert (
        different.status_code == 409
        and different.json()["detail"]["code"] == "idempotency_conflict"
    )
    assert post(client, url, row).status_code == 503
    client.cookies.clear()
    assert client.get(resource).status_code == 401
    assert snapshot(s) == before
