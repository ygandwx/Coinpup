"""A deterministic Logo defect cannot consume an Invoice number or key."""

import json
import struct
from uuid import uuid4
from zlib import compress, crc32

import pytest
from coinpup_api.business import invoice_issuance_service
from coinpup_api.business.invoice_issuance_schemas import InvoiceIssuanceCreate
from coinpup_api.business.invoice_logo import MAX_LOGO_DIMENSION
from coinpup_api.business.invoice_profile_models import InvoiceProfile
from coinpup_api.business.invoice_reservation_command import InvoiceReservationCommand
from coinpup_api.business.invoice_reservation_inputs import FreshInvoiceLogo, InvoiceLogoCheck
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.sync.locking import acquire_write_lock
from sqlalchemy import update
from sqlalchemy.orm import Session

from tests.integration.business.test_invoice_issuance_http import path, post
from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_render_sources import PNG
from tests.integration.business.test_invoice_snapshot_binding import company as company
from tests.integration.business.test_invoice_snapshot_binding import reviewed
from tests.integration.conftest import authenticated_client as authenticated_client
from tests.integration.conftest import structure_database as structure_database
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.invoice_database.test_http_originals import scoped_store as scoped_store

pytestmark = pytest.mark.integration


def wide_png():
    def chunk(name, data):
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", crc32(name + data))

    width = MAX_LOGO_DIMENSION + 1
    return (
        PNG[:8]
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", compress(bytes(1 + 4 * width)))
        + chunk(b"IEND", b"")
    )


def select_logo(client, ledger, configuration_id, version, content):
    base = f"/api/v1/ledgers/{ledger}"
    opened = client.post(
        base + "/uploads",
        json={
            "id": str(uuid4()),
            "original_filename": "fictional-logo.png",
            "declared_size": len(content),
        },
    )
    assert opened.status_code == 201, opened.text
    stored = client.put(
        f"{base}/uploads/{opened.json()['id']}/content",
        content=content,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert stored.status_code == 200, stored.text
    file = stored.json()
    assert (file["byte_size"], file["media_type"]) == (len(content), "image/png")
    chosen = client.put(
        f"{base}/invoice-profiles/{configuration_id}",
        json={
            "expected_version": version,
            "name": "Fictional reviewed logo",
            "numbering": {"prefix": "虚构-", "minimum_digits": 6},
            "logo_file_id": file["file_id"],
            "logo_file_version": 1,
        },
    )
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["logo_file_id"] == file["file_id"]
    return chosen.json()


@pytest.fixture
def logo_case(scoped_store, authenticated_client, company):
    s = company
    client, engine, owner = authenticated_client
    assert engine is s["engine"] and owner == s["owner"]
    return s, client, reviewed(s)


def test_under_byte_cap_invalid_logo_leaves_number_and_key_available(logo_case):
    s, client, row = logo_case
    url = path(s["ledger"])
    original = json.loads(row["request_json"])
    image = wide_png()
    assert 0 < len(image) < 1024
    bad = select_logo(client, s["ledger"], row["configuration_id"], 1, image)
    original["expected_configuration_version"] = bad["version"]
    before = snapshot(s)
    failed = post(client, url, row, content=json.dumps(original).encode())
    after = snapshot(s)
    observed = client.get(f"{url}/{original['id']}")
    reserved = [
        (r["sequence"], r["idempotency_key"]) for r in after["business_invoice_reservations"]
    ]
    assert (
        failed.status_code,
        failed.json()["detail"]["code"],
        observed.status_code,
        reserved,
    ) == (
        422,
        "invoice_logo_invalid",
        404,
        [],
    )
    assert failed.headers["cache-control"] == "no-store" and after == before

    good = select_logo(client, s["ledger"], row["configuration_id"], bad["version"], PNG)
    original["expected_configuration_version"] = good["version"]
    issued = post(client, url, row, content=json.dumps(original).encode())
    assert issued.status_code == 201, issued.text
    complete = issued.json()
    assert (complete["sequence"], complete["number"], complete["state"]) == (
        "1",
        row["number"],
        "complete",
    )
    before = snapshot(s)
    assert post(client, url, row, content=json.dumps(original).encode()).content == issued.content
    assert snapshot(s) == before


def test_legacy_pending_logo_replay_never_uses_fresh_preflight(logo_case, monkeypatch):
    s, client, row = logo_case
    profile = select_logo(client, s["ledger"], row["configuration_id"], 1, wide_png())
    command = json.loads(row["request_json"]) | {
        "expected_configuration_version": profile["version"]
    }
    body = json.dumps(command).encode()
    # Model an original reservation created before the fresh preflight existed.
    reserve = InvoiceReservationCommand(s["engine"]).reserve
    intent = (
        s["owner"],
        s["ledger"],
        InvoiceIssuanceCreate.model_validate(command),
        row["idempotency_key"],
    )
    logo = reserve(*intent, raw_body=body)
    assert isinstance(logo, FreshInvoiceLogo)
    pending = reserve(*intent, raw_body=body, logo_check=InvoiceLogoCheck(logo, True))
    assert pending.state == "pending" and pending.number == row["number"]

    def unexpected_check(*args):
        pytest.fail("An existing reservation must use its frozen Logo")

    monkeypatch.setattr(invoice_issuance_service, "check_invoice_logo", unexpected_check)
    before = snapshot(s)
    url = path(s["ledger"])
    replay = post(client, url, row, content=body)
    assert (replay.status_code, replay.json()["detail"]["code"]) == (503, "invoice_render_limit")
    observed = client.get(f"{url}/{command['id']}")
    assert observed.status_code == 200 and observed.json() == pending.model_dump(mode="json")
    assert snapshot(s) == before


@pytest.mark.parametrize("outcome", ["profile-change", "time-limit"])
def test_logo_preflight_does_not_hold_the_writer_lock_or_turn_infrastructure_into_rejection(
    logo_case, monkeypatch, outcome
):
    s, client, row = logo_case
    profile = select_logo(client, s["ledger"], row["configuration_id"], 1, PNG)
    command = json.loads(row["request_json"]) | {
        "expected_configuration_version": profile["version"]
    }
    actual_check = invoice_issuance_service.check_invoice_logo

    def concurrent_check(content, identity):
        if outcome == "time-limit":
            raise MoneyError("invoice_render_timeout", "Fictional isolated decoder timeout")
        assert actual_check(content, identity)
        with Session(s["engine"]) as session, session.begin():
            session.connection().exec_driver_sql("SET LOCAL lock_timeout = '1s'")
            acquire_write_lock(session)
            session.execute(
                update(InvoiceProfile)
                .where(InvoiceProfile.id == row["configuration_id"])
                .values(version=profile["version"] + 1, name="Fictional changed profile")
            )
        return True

    monkeypatch.setattr(invoice_issuance_service, "check_invoice_logo", concurrent_check)
    before = snapshot(s)
    url = path(s["ledger"])
    response = post(client, url, row, content=json.dumps(command).encode())
    expected = (
        (409, "version_conflict")
        if outcome == "profile-change"
        else (503, "invoice_render_timeout")
    )
    assert (response.status_code, response.json()["detail"]["code"]) == expected
    assert client.get(f"{url}/{command['id']}").status_code == 404
    after = snapshot(s)
    if outcome == "profile-change":
        # Only the separate profile update and its notification may survive this request.
        for name in ("business_invoice_profiles", "change_log"):
            assert after.pop(name) != before.pop(name)
    assert after == before
