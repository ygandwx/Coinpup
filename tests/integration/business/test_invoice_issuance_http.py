"""Real browser session, original raw intent, scope and no-store boundaries."""

import json
import struct
from uuid import uuid4
from zlib import crc32

import pytest
from coinpup_api.business.invoice_logo import MAX_LOGO_BYTES
from coinpup_api.business.invoice_reservation_command import InvoiceReservationCommand
from coinpup_api.ledger.schemas import EntityCreate

from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_render_sources import PNG
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


def test_private_logo_size_is_checked_before_reserving_the_original_key_and_number(http_case):
    s, client, row = http_case
    ledger_path = f"/api/v1/ledgers/{s['ledger']}"
    url = path(s["ledger"])
    profile_url = f"{ledger_path}/invoice-profiles/{row['configuration_id']}"
    command = json.loads(row["request_json"])

    def png_at_size(size):
        # Add a valid ancillary text chunk before IEND without increasing image dimensions.
        text = b"Fictional\x00".ljust(size - len(PNG) - 12, b"x")
        chunk = b"tEXt" + text
        content = PNG[:-12] + struct.pack(">I", len(text)) + chunk
        return content + struct.pack(">I", crc32(chunk)) + PNG[-12:]

    def select_logo(content, expected_version):
        uploaded = client.post(
            f"{ledger_path}/uploads",
            json={
                "id": str(uuid4()),
                "original_filename": "fictional-logo.png",
                "declared_size": len(content),
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        stored = client.put(
            f"{ledger_path}/uploads/{uploaded.json()['id']}/content",
            content=content,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert stored.status_code == 200, stored.text
        original = stored.json()
        assert (original["byte_size"], original["media_type"]) == (len(content), "image/png")
        chosen = client.put(
            profile_url,
            json={
                "expected_version": expected_version,
                "name": "Fictional reviewed logo",
                "numbering": {"prefix": "虚构-", "minimum_digits": 6},
                "logo_file_id": original["file_id"],
                "logo_file_version": 1,
            },
        )
        assert chosen.status_code == 200, chosen.text
        assert chosen.json()["logo_file_id"] == original["file_id"]
        return chosen.json()["version"]

    oversized_logo = png_at_size(MAX_LOGO_BYTES + 1)
    assert len(oversized_logo) == MAX_LOGO_BYTES + 1
    command["expected_configuration_version"] = select_logo(oversized_logo, 1)
    before = snapshot(s)
    denied = post(client, url, row, content=json.dumps(command).encode())
    after = snapshot(s)
    stored = client.get(f"{url}/{command['id']}")
    reservations = [
        (item["sequence"], item["number"], item["idempotency_key"])
        for item in after["business_invoice_reservations"]
    ]
    assert (
        denied.status_code,
        denied.json()["detail"]["code"],
        stored.status_code,
        reservations,
    ) == (
        422,
        "invoice_logo_size_limit",
        404,
        [],
    )
    assert denied.headers["cache-control"] == "no-store" and after == before

    # A smaller Logo can use the same key and intent after a fresh profile review.
    command["expected_configuration_version"] = select_logo(PNG, 2)
    accepted = post(client, url, row, content=json.dumps(command).encode())
    assert accepted.status_code == 503  # This fixture deliberately has no rendering fonts.
    saved = client.get(f"{url}/{command['id']}")
    assert saved.status_code == 200
    assert (saved.json()["sequence"], saved.json()["number"], saved.json()["state"]) == (
        "1",
        row["number"],
        "pending",
    )

    # The exact cap is admissible for another confirmed document and advances by one.
    boundary_logo = png_at_size(MAX_LOGO_BYTES)
    assert len(boundary_logo) == MAX_LOGO_BYTES
    profile_version = select_logo(boundary_logo, 3)
    second, _ = confirmed(s, "invoice", 1, key="fictional-confirm-boundary-logo")
    second_id = str(uuid4())
    boundary = post(
        client,
        url,
        row | {"idempotency_key": "fictional-boundary-logo-reservation"},
        content=json.dumps(
            command
            | {
                "id": second_id,
                "document_id": str(second.id),
                "expected_document_version": second.version,
                "expected_configuration_version": profile_version,
            }
        ).encode(),
    )
    assert boundary.status_code == 503  # The exact-size Logo reached rendering as well.
    saved = client.get(f"{url}/{second_id}")
    assert saved.status_code == 200
    assert (saved.json()["sequence"], saved.json()["number"], saved.json()["state"]) == (
        "2",
        "虚构-000002",
        "pending",
    )
