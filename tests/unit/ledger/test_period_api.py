"""Period HTTP endpoints enforce authentication, CSRF, bounds and original body forwarding."""

import json
from uuid import UUID

import pytest
from coinpup_api.ledger.period_reads import PeriodReads
from coinpup_api.ledger.period_service import PeriodService
from coinpup_api.ledger.service import LedgerError

from tests.unit.ledger.test_posting_api import LEDGER, ORIGIN, OWNER, RECORD
from tests.unit.ledger.test_posting_api import client as client
from tests.unit.ledger.test_posting_api import signed_in as signed_in

BODY = {
    "action": "close",
    "closed_through": "2026-01-31",
    "expected_version": 1,
    "reason": " Fictional close ",
}
RESPONSE = {
    "id": RECORD,
    "ledger_id": RECORD,
    "actor_id": str(OWNER),
    "version": 2,
    "action": "close",
    "reason": "Fictional close",
    "previous_closed_through": None,
    "closed_through": "2026-01-31",
    "created_at": "2026-01-31T12:00:00Z",
}


@pytest.mark.parametrize(
    "method,path", [("GET", "/period"), ("GET", "/period-changes"), ("POST", "/period-changes")]
)
def test_period_endpoints_require_session(client, method, path):
    response = client.request(
        method,
        LEDGER + path,
        json=BODY,
        headers={"Idempotency-Key": "fictional-close", "Origin": ORIGIN},
    )
    assert response.status_code == 401


def test_period_command_forwards_exact_raw_body_and_scope(signed_in, monkeypatch):
    seen = []

    def change(self, *args, **kwargs):
        seen.append((args, kwargs))
        return RESPONSE

    monkeypatch.setattr(PeriodService, "change", change)
    raw = json.dumps(BODY, indent=2).encode()
    response = signed_in.post(
        LEDGER + "/period-changes",
        content=raw,
        headers={"Content-Type": "application/json", "Idempotency-Key": "fictional-close"},
    )
    assert response.status_code == 201 and response.json() == RESPONSE
    args, kwargs = seen[0]
    assert args[:2] == (OWNER, UUID(RECORD)) and args[3] == "fictional-close"
    assert args[2].reason == "Fictional close" and kwargs["raw_body"] == raw


@pytest.mark.parametrize(
    "header,value", [("X-CSRF-Token", "wrong"), ("Origin", "https://fictional.invalid")]
)
def test_period_write_requires_origin_and_csrf(signed_in, header, value):
    assert (
        signed_in.post(
            LEDGER + "/period-changes",
            json=BODY,
            headers={"Idempotency-Key": "fictional-close", header: value},
        ).status_code
        == 403
    )


@pytest.mark.parametrize("query", [{"limit": 201}, {"offset": -1}, {"offset": 100001}])
def test_period_history_pagination_is_bounded(signed_in, monkeypatch, query):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid history query reached storage")

    monkeypatch.setattr(PeriodReads, "history", unexpected)
    assert signed_in.get(LEDGER + "/period-changes", params=query).status_code == 422


def test_period_error_does_not_expose_storage_details(signed_in, monkeypatch):
    def conflict(*args, **kwargs):
        raise LedgerError("version_conflict", 409, "Reload the period.")

    monkeypatch.setattr(PeriodService, "change", conflict)
    response = signed_in.post(
        LEDGER + "/period-changes", json=BODY, headers={"Idempotency-Key": "fictional-close"}
    )
    assert response.status_code == 409 and response.json()["detail"]["code"] == "version_conflict"
