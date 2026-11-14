"""Refund revision routes keep the existing session, raw-intent and error contracts."""

import json
from uuid import UUID, uuid4

import pytest
from coinpup_api.business.refund_revision_schemas import RefundRevisionResponse
from coinpup_api.business.refund_revisions import BusinessRefundRevisionService
from coinpup_api.ledger.service import LedgerError
from sqlalchemy.exc import SQLAlchemyError

from tests.unit.business.test_refund_revision_contract import response as receipt
from tests.unit.business.test_refund_revision_replay import intent
from tests.unit.business.test_settlement_router import HEADERS
from tests.unit.ledger.test_ledger_api import LEDGER, OWNER, RECORD
from tests.unit.ledger.test_ledger_api import client as client
from tests.unit.ledger.test_ledger_api import signed_in as signed_in

REFUND = uuid4()
PATHS = {"correct": "corrections", "cancel": "cancellations"}


def example(action):
    result = RefundRevisionResponse.model_validate(
        receipt(action) | dict(ledger_id=RECORD, refund_id=str(REFUND))
    )
    return intent(result).model_dump(mode="json"), result


def path(action):
    return f"{LEDGER}/business-refunds/{REFUND}/{PATHS[action]}"


@pytest.mark.parametrize("action", PATHS)
def test_revision_auth_and_exact_raw_bytes_reach_only_the_scoped_service(
    signed_in, monkeypatch, action
):
    body, result = example(action)
    calls = []

    def capture(self, owner, ledger, refund, payload, key, *, raw_body):
        calls.append((owner, ledger, refund, payload.model_dump(mode="json"), key, raw_body))
        return result

    monkeypatch.setattr(BusinessRefundRevisionService, action, capture)
    for omitted in ("origin", "x-csrf-token"):
        response = signed_in.post(
            path(action), json=body, headers={k: v for k, v in HEADERS.items() if k != omitted}
        )
        assert response.status_code == 403
        assert response.headers["cache-control"] == "no-store"
    assert calls == []
    raw = json.dumps(body | {"reason": "  " + body["reason"] + "  "}, indent=2).encode()
    response = signed_in.post(path(action), content=raw, headers=HEADERS)
    assert response.status_code == 200 and response.json() == result.model_dump(mode="json")
    assert response.headers["cache-control"] == "no-store"
    assert calls == [(OWNER, UUID(RECORD), REFUND, body, HEADERS["Idempotency-Key"], raw)]
    signed_in.cookies.clear()
    denied = signed_in.post(path(action), content=raw, headers=HEADERS)
    assert denied.status_code == 401 and denied.headers["cache-control"] == "no-store"
    assert len(calls) == 1


@pytest.mark.parametrize("action", PATHS)
def test_invalid_key_path_and_body_never_call_revision_service(signed_in, monkeypatch, action):
    body, _ = example(action)

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid command reached the financial service")

    monkeypatch.setattr(BusinessRefundRevisionService, action, forbidden)
    for key in (None, "", "bad key", "x" * 129):
        headers = {k: v for k, v in HEADERS.items() if k != "Idempotency-Key"}
        if key is not None:
            headers["Idempotency-Key"] = key
        response = signed_in.post(path(action), json=body, headers=headers)
        assert response.status_code == 422
        assert response.headers["cache-control"] == "no-store"
    invalid = [
        body | {"expected_version": True},
        body | {"expected_version": "5"},
        body | {"owner_id": str(OWNER)},
        body | {"reason": ""},
    ]
    if action == "correct":
        for changes in (
            {"payment_id": str(uuid4())},
            {"amount": 1},
            {"fees": [{}]},
            {"allocation_return_ids": [str(REFUND)] * 2},
        ):
            invalid.append(body | {"replacement": body["replacement"] | changes})
    else:
        invalid.append(body | {"replacement": None})
    for value in invalid:
        response = signed_in.post(path(action), json=value, headers=HEADERS)
        assert response.status_code == 422
        assert all(set(item) == {"loc", "type", "msg"} for item in response.json()["detail"])
    for url, raw in (
        (path(action).replace(str(REFUND), "invalid"), json.dumps(body)),
        (path(action), "{"),
    ):
        response = signed_in.post(url, content=raw, headers=HEADERS)
        assert response.status_code == 422 and response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("action", PATHS)
@pytest.mark.parametrize(
    "status,code",
    [
        (404, "not_found"),
        (409, "version_conflict"),
        (422, "amount_precision"),
        (503, "refund_revision_integrity"),
    ],
)
def test_revision_domain_errors_keep_status_and_code(signed_in, monkeypatch, action, status, code):
    body, _ = example(action)

    def fail(self, owner, ledger, refund, payload, key, *, raw_body):
        assert (owner, ledger, refund) == (OWNER, UUID(RECORD), REFUND)
        raise LedgerError(code, status, "Fictional safe domain message")

    monkeypatch.setattr(BusinessRefundRevisionService, action, fail)
    response = signed_in.post(path(action), json=body, headers=HEADERS)
    assert response.status_code == status
    assert response.json()["detail"] == dict(code=code, message="Fictional safe domain message")
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("action", PATHS)
def test_revision_database_errors_do_not_disclose_private_details(signed_in, monkeypatch, action):
    body, _ = example(action)

    def fail(*args, **kwargs):
        raise SQLAlchemyError("Fictional private SQL and connection password must never escape")

    monkeypatch.setattr(BusinessRefundRevisionService, action, fail)
    response = signed_in.post(path(action), json=body, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "business_unavailable"
    assert "password" not in response.text and "SQL" not in response.text
    assert response.headers["cache-control"] == "no-store"
