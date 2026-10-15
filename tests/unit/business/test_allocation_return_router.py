"""Return HTTP scope, original bytes, auth and sanitized failure contracts."""

import json
from uuid import UUID, uuid4

import pytest
from coinpup_api.business.allocation_return_reads import BusinessAllocationReturnReads
from coinpup_api.business.allocation_return_schemas import BusinessAllocationReturnResponse
from coinpup_api.business.allocation_returns import BusinessAllocationReturnService
from coinpup_api.ledger.service import LedgerError
from sqlalchemy.exc import SQLAlchemyError

from tests.unit.business.test_settlement_router import HEADERS
from tests.unit.ledger.test_ledger_api import LEDGER, OWNER, RECORD
from tests.unit.ledger.test_ledger_api import client as client
from tests.unit.ledger.test_ledger_api import signed_in as signed_in

BODY = dict(
    id=str(uuid4()),
    payment_id=str(uuid4()),
    return_date="2026-10-01",
    reason="Fictional return 中文",
    lines=[
        dict(
            source_allocation_line_id=str(uuid4()),
            expected_version=1,
            payment_amount="22.00",
            document_amount="20",
        )
    ],
)
RESULT = dict(
    kind="business_allocation_return",
    id=BODY["id"],
    ledger_id=RECORD,
    payment_id=BODY["payment_id"],
    return_date=BODY["return_date"],
    postings=[
        dict(
            line_no=1,
            source_allocation_line_id=BODY["lines"][0]["source_allocation_line_id"],
            source_operation_id=str(uuid4()),
            document_id=str(uuid4()),
            document_line_id=str(uuid4()),
            operation_id=str(uuid4()),
            journal_id=str(uuid4()),
            payment_amount="22.000000000000000000",
            document_amount="20.000000000000000000",
        )
    ],
)
PATH = LEDGER + "/business-allocation-returns"
READS = [
    ("business-allocation-returns", "list_returns"),
    (f"business-allocation-returns/{RECORD}", "get_return"),
    (f"business-allocations/{RECORD}/return-capacity", "capacity"),
]


@pytest.mark.parametrize("path,method", READS)
def test_return_reads_require_private_session(client, path, method):
    response = client.get(LEDGER + "/" + path)
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"


def test_write_auth_and_original_bytes(signed_in, monkeypatch):
    calls = []

    def capture(self, owner, ledger, payload, key, *, raw_body):
        calls.append((owner, ledger, payload.model_dump(mode="json"), key, raw_body))
        return BusinessAllocationReturnResponse.model_validate(RESULT)

    monkeypatch.setattr(BusinessAllocationReturnService, "create", capture)
    for omitted in ("origin", "x-csrf-token"):
        response = signed_in.post(
            PATH, json=BODY, headers={k: v for k, v in HEADERS.items() if k != omitted}
        )
        assert response.status_code == 403
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
def test_invalid_key_cannot_call_service(signed_in, monkeypatch, key):
    def unexpected(*args, **kwargs):
        raise AssertionError("Invalid key reached return service")

    monkeypatch.setattr(BusinessAllocationReturnService, "create", unexpected)
    headers = {k: v for k, v in HEADERS.items() if k != "Idempotency-Key"}
    if key is not None:
        headers["Idempotency-Key"] = key
    assert signed_in.post(PATH, json=BODY, headers=headers).status_code == 422


def test_return_list_pagination_scope_and_filter(signed_in, monkeypatch):
    calls = []

    def capture(self, owner, ledger, **kwargs):
        calls.append((owner, ledger, kwargs))
        return []

    monkeypatch.setattr(BusinessAllocationReturnReads, "list_returns", capture)
    assert signed_in.get(PATH).json() == []
    assert calls == [(OWNER, UUID(RECORD), dict(payment_id=None, limit=100, offset=0))]
    for query in ("limit=0", "limit=201", "offset=-1", "offset=100001", "payment_id=bad"):
        assert signed_in.get(PATH + "?" + query).status_code == 422
    assert len(calls) == 1
    assert signed_in.get(PATH + "?payment_id=" + RECORD).json() == []
    assert calls[-1][2]["payment_id"] == UUID(RECORD)


@pytest.mark.parametrize("path,method", READS + [("business-allocation-returns", "create")])
@pytest.mark.parametrize("storage", [True, False])
def test_errors_are_safe_for_reads_and_writes(signed_in, monkeypatch, path, method, storage):
    def fail(*args, **kwargs):
        if storage:
            raise SQLAlchemyError("Fictional private DSN")
        raise LedgerError("allocation_return_integrity", 503, "The stored return is inconsistent.")

    target = (
        BusinessAllocationReturnService if method == "create" else BusinessAllocationReturnReads
    )
    monkeypatch.setattr(target, method, fail)
    response = (
        signed_in.post(PATH, json=BODY, headers=HEADERS)
        if method == "create"
        else signed_in.get(LEDGER + "/" + path)
    )
    assert response.status_code == 503 and "private DSN" not in response.text
    assert response.json()["detail"]["code"] == (
        "business_unavailable" if storage else "allocation_return_integrity"
    )
