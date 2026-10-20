"""Refund HTTP preserves private scope, raw original bytes and sanitized failure responses."""

import json
from uuid import UUID, uuid4

import pytest
from coinpup_api.business.refund_reads import BusinessRefundReads
from coinpup_api.business.refund_schemas import BusinessRefundResponse
from coinpup_api.business.refunds import BusinessRefundService
from coinpup_api.ledger.service import LedgerError
from sqlalchemy.exc import SQLAlchemyError

from tests.unit.business.test_settlement_router import HEADERS
from tests.unit.ledger.test_ledger_api import LEDGER, OWNER, RECORD
from tests.unit.ledger.test_ledger_api import client as client
from tests.unit.ledger.test_ledger_api import signed_in as signed_in

BODY = dict(
    id=str(uuid4()),
    payment_id=str(uuid4()),
    expected_version=1,
    account_id=str(uuid4()),
    asset_id="EUR",
    amount="19.37",
    payment_amount="20",
    transaction_date="2026-10-02",
    reason="Fictional refund 中文",
)
RESULT = dict(
    kind="business_refund",
    id=BODY["id"],
    ledger_id=RECORD,
    payment_id=BODY["payment_id"],
    operation_id=str(uuid4()),
    journal_id=str(uuid4()),
    account_id=BODY["account_id"],
    asset_id="EUR",
    amount="19.37",
    payment_asset_id="USD",
    payment_amount="20",
    transaction_date=BODY["transaction_date"],
    allocation_return_ids=[],
    fees=[],
)
PATH = LEDGER + "/business-refunds"
READS = [
    ("business-refunds", "list_refunds"),
    (f"business-refunds/{RECORD}", "get_refund"),
    (f"business-payments/{RECORD}/refund-capacity", "capacity"),
]


@pytest.mark.parametrize("path,method", READS)
def test_refund_reads_are_private(client, path, method):
    response = client.get(LEDGER + "/" + path)
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"


def test_write_auth_raw_bytes_and_no_store(signed_in, monkeypatch):
    calls = []

    def capture(self, owner, ledger, payload, key, *, raw_body):
        calls.append(
            (owner, ledger, payload.model_dump(mode="json", exclude_unset=True), key, raw_body)
        )
        return BusinessRefundResponse.model_validate(RESULT)

    monkeypatch.setattr(BusinessRefundService, "create", capture)
    for omitted in ("origin", "x-csrf-token"):
        assert (
            signed_in.post(
                PATH, json=BODY, headers={k: v for k, v in HEADERS.items() if k != omitted}
            ).status_code
            == 403
        )
    assert not calls
    raw = json.dumps(BODY | {"id": BODY["id"].upper()}, indent=2).encode()
    response = signed_in.post(PATH, content=raw, headers=HEADERS)
    assert response.status_code == 201 and response.json() == RESULT
    assert response.headers["cache-control"] == "no-store"
    assert calls == [(OWNER, UUID(RECORD), BODY, HEADERS["Idempotency-Key"], raw)]
    signed_in.cookies.clear()
    assert signed_in.post(PATH, json=BODY, headers=HEADERS).status_code == 401
    assert len(calls) == 1


@pytest.mark.parametrize("key", [None, "", "bad key", "x" * 129])
def test_invalid_key_never_calls_refund_service(signed_in, monkeypatch, key):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid command reached service")

    monkeypatch.setattr(BusinessRefundService, "create", forbidden)
    headers = {k: v for k, v in HEADERS.items() if k != "Idempotency-Key"}
    if key is not None:
        headers["Idempotency-Key"] = key
    assert signed_in.post(PATH, json=BODY, headers=headers).status_code == 422


@pytest.mark.parametrize("path,method", READS)
@pytest.mark.parametrize("storage_error", [False, True])
def test_read_failures_are_sanitized(signed_in, monkeypatch, path, method, storage_error):
    def fail(self, owner, ledger, *args, **kwargs):
        assert (owner, ledger) == (OWNER, UUID(RECORD))
        if storage_error:
            raise SQLAlchemyError("fictional database password must never escape")
        raise LedgerError("not_found", 404, "The record was not found.")

    monkeypatch.setattr(BusinessRefundReads, method, fail)
    response = signed_in.get(LEDGER + "/" + path)
    assert response.status_code == (503 if storage_error else 404)
    assert "password" not in response.text


def test_list_filter_page_scope_and_boundaries(signed_in, monkeypatch):
    calls = []

    def capture(self, owner, ledger, **kwargs):
        calls.append((owner, ledger, kwargs))
        return []

    monkeypatch.setattr(BusinessRefundReads, "list_refunds", capture)
    assert signed_in.get(PATH + f"?payment_id={RECORD}&limit=5&offset=2").json() == []
    assert calls == [(OWNER, UUID(RECORD), dict(payment_id=UUID(RECORD), limit=5, offset=2))]
    for query in ("limit=0", "limit=201", "offset=-1", "offset=100001", "payment_id=invalid"):
        assert signed_in.get(PATH + "?" + query).status_code == 422
    assert len(calls) == 1
