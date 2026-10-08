"""Private HTTP boundary forwards raw intent and never exposes internal document data."""

import json

import pytest
from coinpup_api.ledger.service import LedgerError
from coinpup_api.ocr.confirmation import ConfirmationService
from coinpup_api.ocr.confirmation_reads import ConfirmationReadService
from sqlalchemy.exc import OperationalError

from tests.unit.ocr.test_confirmation_contracts import raw
from tests.unit.ocr.test_ocr_router import HEADERS, LEDGER, OTHER, OWNER, SQL, TOKEN, forbid_service
from tests.unit.ocr.test_ocr_router import client as client

PREFIX = f"/api/v1/ledgers/{LEDGER}/ocr-drafts/{OTHER}"


@pytest.mark.parametrize(
    "suffix,method", [("confirmation", "get_confirmation"), ("duplicates", "list_duplicates")]
)
def test_confirmation_reads_require_session_before_storage(client, monkeypatch, suffix, method):
    monkeypatch.setattr(ConfirmationReadService, method, forbid_service)
    response = client.get(f"{PREFIX}/{suffix}")
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "session,headers,status",
    [
        (False, HEADERS, 401),
        (True, {}, 403),
        (True, {**HEADERS, "origin": "https://fictional.invalid"}, 403),
        (True, {**HEADERS, "x-csrf-token": "fictional-invalid"}, 403),
    ],
)
def test_confirmation_origin_session_csrf_guard_financial_service(
    client, monkeypatch, session, headers, status
):
    monkeypatch.setattr(ConfirmationService, "confirm", forbid_service)
    if session:
        client.cookies.set("coinpup_session", TOKEN)
    assert client.post(f"{PREFIX}/confirmations", json=raw(), headers=headers).status_code == status


def test_original_bytes_and_authenticated_owner_reach_confirmation_service(client, monkeypatch):
    client.cookies.set("coinpup_session", TOKEN)
    value = raw()
    encoded = json.dumps(value, indent=3).encode()
    received = []

    def confirm(self, owner, ledger, draft, payload, *, raw_body):
        received.append((owner, ledger, draft, payload, raw_body))
        raise LedgerError("fictional_stop", 409, "Fictional boundary proof")

    monkeypatch.setattr(ConfirmationService, "confirm", confirm)
    response = client.post(
        f"{PREFIX}/confirmations",
        content=encoded,
        headers={**HEADERS, "content-type": "application/json"},
    )
    assert response.status_code == 409 and response.json()["detail"]["code"] == "fictional_stop"
    assert received[0][:3] == (OWNER, LEDGER, OTHER) and received[0][4] == encoded
    assert received[0][3].entry.command.amount == "1.00"
    assert "duplicate_ack" not in received[0][3].model_fields_set


def test_invalid_financial_input_is_redacted_before_confirmation(client, monkeypatch):
    client.cookies.set("coinpup_session", TOKEN)
    monkeypatch.setattr(ConfirmationService, "confirm", forbid_service)
    value = raw()
    value["entry"]["command"]["amount"] = "Fictional private amount"
    response = client.post(f"{PREFIX}/confirmations", json=value, headers=HEADERS)
    assert response.status_code == 422 and "Fictional private" not in response.text


@pytest.mark.parametrize(
    "suffix,method",
    [
        ("confirmation", "get_confirmation"),
        ("duplicates", "list_duplicates"),
        ("confirmations", "confirm"),
    ],
)
def test_database_failures_never_expose_sql_or_original_text(client, monkeypatch, suffix, method):
    client.cookies.set("coinpup_session", TOKEN)

    def fail(*args, **kwargs):
        raise OperationalError(SQL, {}, RuntimeError("Fictional private document"))

    monkeypatch.setattr(
        ConfirmationService if method == "confirm" else ConfirmationReadService, method, fail
    )
    response = (
        client.post(f"{PREFIX}/{suffix}", json=raw(), headers=HEADERS)
        if method == "confirm"
        else client.get(f"{PREFIX}/{suffix}")
    )
    assert response.status_code == 503 and response.json()["detail"]["code"] == "ocr_unavailable"
    assert SQL not in response.text and "Fictional" not in response.text
