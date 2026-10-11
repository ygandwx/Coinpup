"""Fictional payment/allocation HTTP intent, auth, pagination and safe failure boundaries."""

import json
from uuid import UUID, uuid4

import pytest
from coinpup_api.business.settlement_reads import BusinessSettlementReads
from coinpup_api.business.settlement_schemas import (
    BusinessAllocationResponse,
    BusinessPaymentResponse,
)
from coinpup_api.business.settlements import BusinessSettlementService
from coinpup_api.ledger.service import LedgerError
from coinpup_api.security import csrf_token_for
from sqlalchemy.exc import SQLAlchemyError

from tests.unit.business.test_settlement_plan import payment_command
from tests.unit.ledger.test_ledger_api import LEDGER, ORIGIN, OWNER, RECORD, TOKEN
from tests.unit.ledger.test_ledger_api import client as client
from tests.unit.ledger.test_ledger_api import signed_in as signed_in

PAYMENT = payment_command().model_dump(mode="json")
ALLOCATION = dict(
    id=str(uuid4()),
    payment_id=PAYMENT["id"],
    allocation_date="2026-09-30",
    lines=[
        dict(
            document_id=RECORD,
            document_line_id=str(uuid4()),
            payment_amount="11",
            document_amount="10",
        )
    ],
)
PAYMENT_RESULT = dict(
    kind="business_payment",
    id=PAYMENT["id"],
    ledger_id=RECORD,
    operation_id=str(uuid4()),
    journal_id=str(uuid4()),
    initial_allocation=None,
)
ALLOCATION_RESULT = dict(
    kind="business_allocation",
    id=ALLOCATION["id"],
    ledger_id=RECORD,
    payment_id=PAYMENT["id"],
    postings=[
        dict(
            line_no=1,
            document_id=RECORD,
            document_line_id=ALLOCATION["lines"][0]["document_line_id"],
            operation_id=str(uuid4()),
            journal_id=str(uuid4()),
        )
    ],
)
COMMANDS = [
    ("business-payments", "pay", PAYMENT, PAYMENT_RESULT),
    ("business-allocations", "allocate", ALLOCATION, ALLOCATION_RESULT),
]
HEADERS = {
    "origin": ORIGIN,
    "x-csrf-token": csrf_token_for(TOKEN),
    "Idempotency-Key": "fictional-settlement",
    "Content-Type": "application/json",
}
READS = [
    ("business-payments", "list_payments"),
    ("business-allocations", "list_allocations"),
    (f"business-payments/{RECORD}", "get_payment"),
    (f"business-allocations/{RECORD}", "get_allocation"),
    (f"business-documents/{RECORD}/settlement", "document_capacity"),
]


@pytest.mark.parametrize("path,method", READS)
def test_reads_require_session_and_private_cache(client, path, method):
    response = client.get(LEDGER + "/" + path)
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path,method,body,expected", COMMANDS)
def test_write_requires_origin_csrf_and_session(signed_in, path, method, body, expected):
    for omitted, code in (("origin", "origin_not_allowed"), ("x-csrf-token", "csrf_failed")):
        headers = {key: value for key, value in HEADERS.items() if key != omitted}
        response = signed_in.post(LEDGER + "/" + path, json=body, headers=headers)
        assert response.status_code == 403 and response.json()["detail"] == code
    signed_in.cookies.clear()
    assert signed_in.post(LEDGER + "/" + path, json=body, headers=HEADERS).status_code == 401


@pytest.mark.parametrize("path,method,body,expected", COMMANDS)
def test_original_json_bytes_key_and_authenticated_scope_are_preserved(
    signed_in, monkeypatch, path, method, body, expected
):
    calls = []
    raw = json.dumps(body | {"id": body["id"].upper()}, indent=2).encode()

    def capture(self, owner, ledger, payload, key, *, raw_body):
        calls.append((owner, ledger, payload.model_dump(mode="json"), key, raw_body))
        schema = BusinessPaymentResponse if method == "pay" else BusinessAllocationResponse
        return schema.model_validate(expected)

    monkeypatch.setattr(BusinessSettlementService, method, capture)
    response = signed_in.post(LEDGER + "/" + path, content=raw, headers=HEADERS)
    assert response.status_code == 201 and response.json() == expected
    assert response.headers["cache-control"] == "no-store"
    assert calls == [(OWNER, UUID(RECORD), body, "fictional-settlement", raw)]


@pytest.mark.parametrize("path,method,body,expected", COMMANDS)
@pytest.mark.parametrize("key", [None, "", "bad key", "x" * 129])
def test_invalid_key_never_reaches_command(
    signed_in, monkeypatch, path, method, body, expected, key
):
    def unexpected(*args, **kwargs):
        raise AssertionError("Invalid key reached settlement")

    monkeypatch.setattr(BusinessSettlementService, method, unexpected)
    headers = {k: v for k, v in HEADERS.items() if k != "Idempotency-Key"}
    if key is not None:
        headers["Idempotency-Key"] = key
    assert signed_in.post(LEDGER + "/" + path, json=body, headers=headers).status_code == 422


@pytest.mark.parametrize("path,method", READS[:2])
def test_pagination_and_optional_payment_filter(signed_in, monkeypatch, path, method):
    calls = []

    def capture(self, owner, ledger, **options):
        calls.append((owner, ledger, options))
        return []

    monkeypatch.setattr(BusinessSettlementReads, method, capture)
    assert signed_in.get(LEDGER + "/" + path).json() == []
    options = dict(limit=100, offset=0)
    if method == "list_allocations":
        options["payment_id"] = None
    assert calls == [(OWNER, UUID(RECORD), options)]
    for query in ("limit=0", "limit=201", "offset=-1", "offset=100001"):
        assert signed_in.get(LEDGER + "/" + path + "?" + query).status_code == 422
    assert len(calls) == 1
    if method == "list_allocations":
        assert signed_in.get(LEDGER + "/" + path + "?payment_id=" + RECORD).json() == []
        assert calls[-1][2]["payment_id"] == UUID(RECORD)


@pytest.mark.parametrize("path,method", READS)
@pytest.mark.parametrize("database_error", [False, True])
def test_read_failures_have_safe_stable_errors(
    signed_in, monkeypatch, path, method, database_error
):
    def fail(*args, **kwargs):
        if database_error:
            raise SQLAlchemyError("Fictional private connection data")
        raise LedgerError("settlement_integrity", 503, "The stored settlement is inconsistent.")

    monkeypatch.setattr(BusinessSettlementReads, method, fail)
    response = signed_in.get(LEDGER + "/" + path)
    assert response.status_code == 503 and "Fictional private" not in response.text
    assert response.json()["detail"]["code"] == (
        "business_unavailable" if database_error else "settlement_integrity"
    )
