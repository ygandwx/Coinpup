"""Confirmation HTTP boundary preserves original bytes and browser protections."""

import json
from uuid import UUID

import pytest
from coinpup_api.business.confirmation_reads import BusinessConfirmationReads
from coinpup_api.business.confirmation_schemas import BusinessConfirmationResponse
from coinpup_api.business.confirmations import BusinessConfirmationService
from coinpup_api.ledger.service import LedgerError
from coinpup_api.security import csrf_token_for
from sqlalchemy.exc import SQLAlchemyError

from tests.unit.business.test_confirmation_contract import response_payload
from tests.unit.ledger.test_ledger_api import LEDGER, ORIGIN, OWNER, RECORD, TOKEN
from tests.unit.ledger.test_ledger_api import client as client
from tests.unit.ledger.test_ledger_api import signed_in as signed_in

PATH = LEDGER + f"/business-documents/{RECORD}/confirm"
DETAIL = LEDGER + f"/business-documents/{RECORD}/confirmation"
LIST = LEDGER + "/business-confirmations"
BODY = {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "expected_version": 2}
HEADERS = {
    "origin": ORIGIN,
    "x-csrf-token": csrf_token_for(TOKEN),
    "Idempotency-Key": "fictional-confirm",
    "Content-Type": "application/json",
}


@pytest.mark.parametrize("path", [DETAIL, LIST])
def test_read_auth_and_private_cache(client, path):
    response = client.get(path)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


def test_write_requires_origin_csrf_and_session(signed_in):
    for origin in (None, "null", "https://attacker.example"):
        headers = (
            HEADERS | {"origin": origin}
            if origin
            else {k: v for k, v in HEADERS.items() if k != "origin"}
        )
        response = signed_in.post(PATH, json=BODY, headers=headers)
        assert response.status_code == 403 and response.json()["detail"] == "origin_not_allowed"
    for csrf in (None, "wrong"):
        headers = (
            HEADERS | {"x-csrf-token": csrf}
            if csrf
            else {k: v for k, v in HEADERS.items() if k != "x-csrf-token"}
        )
        response = signed_in.post(PATH, json=BODY, headers=headers)
        assert response.status_code == 403 and response.json()["detail"] == "csrf_failed"
    signed_in.cookies.clear()
    assert signed_in.post(PATH, json=BODY, headers=HEADERS).status_code == 401


def test_command_passes_owned_scope_key_and_untouched_json_bytes(signed_in, monkeypatch):
    calls = []
    expected = response_payload() | {"id": BODY["id"], "ledger_id": RECORD, "document_id": RECORD}
    raw = json.dumps(BODY | {"id": BODY["id"].upper()}, indent=2).encode()

    def capture(self, owner, ledger, document, payload, key, *, raw_body):
        calls.append((owner, ledger, document, payload, key, raw_body))
        return BusinessConfirmationResponse.model_validate(expected)

    monkeypatch.setattr(BusinessConfirmationService, "confirm", capture)
    response = signed_in.post(PATH, content=raw, headers=HEADERS)
    assert response.status_code == 201 and response.json() == expected
    assert response.headers["cache-control"] == "no-store"
    assert calls[0][:3] == (OWNER, UUID(RECORD), UUID(RECORD))
    assert calls[0][3].model_dump(mode="json") == BODY
    assert calls[0][4:] == ("fictional-confirm", raw)


@pytest.mark.parametrize("key", [None, "", "bad key", "x" * 129])
def test_invalid_or_missing_key_does_not_call_service(signed_in, monkeypatch, key):
    def unexpected(*args, **kwargs):
        raise AssertionError("Invalid key reached service")

    monkeypatch.setattr(BusinessConfirmationService, "confirm", unexpected)
    headers = (
        HEADERS | {"Idempotency-Key": key}
        if key is not None
        else {k: v for k, v in HEADERS.items() if k != "Idempotency-Key"}
    )
    assert signed_in.post(PATH, json=BODY, headers=headers).status_code == 422


@pytest.mark.parametrize(
    "changes",
    [
        dict(expected_version=True),
        dict(expected_version=2147483647),
        dict(total_amount="110"),
        dict(state="confirmed"),
    ],
)
def test_derived_or_invalid_intent_cannot_reach_service(signed_in, monkeypatch, changes):
    def unexpected(*args, **kwargs):
        raise AssertionError("Invalid intent reached service")

    monkeypatch.setattr(BusinessConfirmationService, "confirm", unexpected)
    assert signed_in.post(PATH, json=BODY | changes, headers=HEADERS).status_code == 422


def test_list_is_owned_and_bounded(signed_in, monkeypatch):
    calls = []

    def capture(self, owner, ledger, **options):
        calls.append((owner, ledger, options))
        return []

    monkeypatch.setattr(BusinessConfirmationReads, "list_confirmations", capture)
    assert signed_in.get(LIST).json() == []
    assert calls[-1] == (OWNER, UUID(RECORD), dict(limit=100, offset=0))
    assert signed_in.get(LIST + "?limit=2&offset=3").json() == []
    assert calls[-1][2] == dict(limit=2, offset=3)
    for query in ("limit=0", "limit=201", "offset=-1", "offset=100001"):
        assert signed_in.get(LIST + "?" + query).status_code == 422
    assert len(calls) == 2


@pytest.mark.parametrize("action", ["confirm", "get_confirmation", "list_confirmations"])
@pytest.mark.parametrize("database_error", [False, True])
def test_errors_keep_safe_stable_contract(signed_in, monkeypatch, action, database_error):
    def fail(*args, **kwargs):
        if database_error:
            raise SQLAlchemyError("Fictional private connection data")
        raise LedgerError("version_conflict", 409, "Reload the reviewed version.")

    target = BusinessConfirmationService if action == "confirm" else BusinessConfirmationReads
    monkeypatch.setattr(target, action, fail)
    response = (
        signed_in.post(PATH, json=BODY, headers=HEADERS)
        if action == "confirm"
        else signed_in.get(DETAIL if action == "get_confirmation" else LIST)
    )
    assert response.status_code == (503 if database_error else 409)
    assert "Fictional private" not in response.text
    assert response.json()["detail"]["code"] == (
        "business_unavailable" if database_error else "version_conflict"
    )
