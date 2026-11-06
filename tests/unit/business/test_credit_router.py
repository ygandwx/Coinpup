"""Credit HTTP preserves private scope, raw original bytes and sanitized failure responses."""

import json
from uuid import UUID, uuid4

import pytest
from coinpup_api.business.credit_reads import BusinessCreditReads
from coinpup_api.business.credit_schemas import BusinessCreditResponse
from coinpup_api.business.credits import BusinessCreditService
from coinpup_api.ledger.service import LedgerError
from sqlalchemy.exc import SQLAlchemyError

from tests.unit.business.test_settlement_router import HEADERS
from tests.unit.ledger.test_ledger_api import LEDGER, OWNER, RECORD
from tests.unit.ledger.test_ledger_api import client as client
from tests.unit.ledger.test_ledger_api import signed_in as signed_in

BODY = dict(
    id=str(uuid4()),
    document_id=str(uuid4()),
    expected_version=2,
    credit_date="2026-10-21",
    reason="Fictional credit 中文",
    lines=[
        dict(
            source_line_id=str(uuid4()),
            net_amount="17.23",
            tax_amount="2.77",
            recognition_date="2026-10-20",
        )
    ],
)
RESULT = dict(
    kind="business_credit",
    id=BODY["id"],
    ledger_id=RECORD,
    document_id=BODY["document_id"],
    source_version=2,
    asset_id="USD",
    credit_date=BODY["credit_date"],
    postings=[
        dict(
            line_no=1,
            **BODY["lines"][0],
            source_operation_id=str(uuid4()),
            total_amount="20.000000000000000000",
            operation_id=str(uuid4()),
            journal_id=str(uuid4()),
        )
    ],
)
PATH = LEDGER + "/business-credits"
READS = [
    ("business-credits", "list_credits"),
    (f"business-credits/{RECORD}", "get_credit"),
    (f"business-documents/{RECORD}/credit-capacity", "capacity"),
]


@pytest.mark.parametrize("path,method", READS)
def test_credit_reads_are_private(client, path, method):
    response = client.get(LEDGER + "/" + path)
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"


def test_write_auth_raw_bytes_and_no_store(signed_in, monkeypatch):
    calls = []

    def capture(self, owner, ledger, payload, key, *, raw_body):
        calls.append(
            (owner, ledger, payload.model_dump(mode="json", exclude_unset=True), key, raw_body)
        )
        return BusinessCreditResponse.model_validate(RESULT)

    monkeypatch.setattr(BusinessCreditService, "create", capture)
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
def test_invalid_key_never_calls_credit_service(signed_in, monkeypatch, key):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid command reached service")

    monkeypatch.setattr(BusinessCreditService, "create", forbidden)
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

    monkeypatch.setattr(BusinessCreditReads, method, fail)
    response = signed_in.get(LEDGER + "/" + path)
    assert response.status_code == (503 if storage_error else 404)
    assert "password" not in response.text


def test_list_filter_page_scope_and_boundaries(signed_in, monkeypatch):
    calls = []

    def capture(self, owner, ledger, **kwargs):
        calls.append((owner, ledger, kwargs))
        return []

    monkeypatch.setattr(BusinessCreditReads, "list_credits", capture)
    assert signed_in.get(PATH + f"?document_id={RECORD}&limit=5&offset=2").json() == []
    assert calls == [(OWNER, UUID(RECORD), dict(document_id=UUID(RECORD), limit=5, offset=2))]
    for query in ("limit=0", "limit=201", "offset=-1", "offset=100001", "document_id=invalid"):
        assert signed_in.get(PATH + "?" + query).status_code == 422
    assert len(calls) == 1


def test_capacity_is_bounded_and_not_a_receipt(signed_in, monkeypatch):
    calls = []

    def capture(self, owner, ledger, document_id, **kwargs):
        calls.append((owner, ledger, document_id, kwargs))
        return dict(
            id=document_id,
            ledger_id=ledger,
            document_kind="invoice",
            party_id=uuid4(),
            asset_id="USD",
            issue_date="2026-01-01",
            expected_version=2,
            lines=[],
        )

    monkeypatch.setattr(BusinessCreditReads, "capacity", capture)
    path = LEDGER + f"/business-documents/{RECORD}/credit-capacity"
    response = signed_in.get(path + "?limit=5&offset=2")
    assert response.status_code == 200 and "receipt" not in response.json()
    assert response.headers["cache-control"] == "no-store"
    assert calls == [(OWNER, UUID(RECORD), UUID(RECORD), dict(limit=5, offset=2))]
    for query in ("limit=0", "limit=201", "offset=-1", "offset=100001"):
        assert signed_in.get(path + "?" + query).status_code == 422
    assert len(calls) == 1
