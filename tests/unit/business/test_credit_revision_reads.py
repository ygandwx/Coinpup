"""Current read projections preserve exact amounts, complete groups and private routes."""

from contextlib import nullcontext
from datetime import date
from decimal import Decimal, localcontext
from types import SimpleNamespace as Record
from uuid import UUID, uuid4

import pytest
from coinpup_api.business.credit_revision_reads import BusinessCreditRevisionReads, _current
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.service import LedgerError
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from tests.unit.ledger.test_ledger_api import LEDGER, OWNER, RECORD
from tests.unit.ledger.test_ledger_api import client as client
from tests.unit.ledger.test_ledger_api import signed_in as signed_in

PATH = LEDGER + f"/business-credits/{RECORD}/current"


def data():
    credit = Record(
        id=uuid4(), ledger_id=uuid4(), credit_date=date(2026, 11, 6), reason="Fictional 中文"
    )
    document = Record(id=uuid4(), document_kind="invoice", party_id=uuid4())
    fact = dict(
        ledger_id=credit.ledger_id,
        credit_id=credit.id,
        document_id=document.id,
        group_version=1,
        revision_id=None,
        line_no=1,
        source_line_id=uuid4(),
        source_operation_id=uuid4(),
        operation_id=uuid4(),
        operation_version=1,
        journal_id=uuid4(),
        net_amount=Decimal("17.23"),
        tax_amount=Decimal("2.77"),
        total_amount=Decimal("20"),
        recognition_date=date(2026, 11, 5),
        credit_date=credit.credit_date,
        credit_reason=credit.reason,
    )
    return credit, document, fact


def test_current_quantities_use_complete_asset_with_low_decimal_context():
    credit, document, fact = data()
    with localcontext() as context:
        context.prec = 2
        current = _current(credit, document, ASSETS["USD"], None, [fact])
        assert current.members[0].model_dump()["total_amount"] == "20.00"
        for value in ("0.000000000000000001", "99999999999999999999.999999999999999999"):
            current = _current(
                credit,
                document,
                ASSETS["ETH"],
                None,
                [
                    fact
                    | {
                        "net_amount": Decimal(value),
                        "tax_amount": Decimal(0),
                        "total_amount": Decimal(value),
                    }
                ],
            )
            assert current.asset_id == "ETH" and current.members[0].total_amount == value


@pytest.mark.parametrize(
    "fault",
    [
        "empty",
        "201",
        "duplicate",
        "position",
        "operation_version",
        "ledger_id",
        "credit_id",
        "document_id",
        "revision_id",
        "group_version",
        "credit_date",
        "credit_reason",
        "negative",
        "zero",
        "sum",
        "precision",
        "cancelled_nonempty",
        "wrong_revision_scope",
    ],
)
def test_invalid_current_projection_is_explicit_integrity_error(fault):
    credit, document, fact = data()
    revision, facts = None, [fact]
    if fault == "empty":
        facts = []
    elif fault == "201":
        facts = [fact | {"line_no": n} for n in range(1, 202)]
    elif fault == "duplicate":
        facts.append(fact | {"line_no": 2})
    elif fault in {"position", "operation_version"}:
        fact["line_no" if fault == "position" else fault] = 0
    elif fault in {"ledger_id", "credit_id", "document_id", "revision_id"}:
        fact[fault] = uuid4()
    elif fault == "group_version":
        fact[fault] = 2
    elif fault == "credit_date":
        fact[fault] = date(2026, 1, 1)
    elif fault == "credit_reason":
        fact[fault] = "Fictional different fact"
    elif fault in {"negative", "zero", "sum", "precision"}:
        fact.update(
            {
                "negative": {"net_amount": Decimal("-1")},
                "zero": {"total_amount": Decimal(0)},
                "sum": {"total_amount": Decimal("21")},
                "precision": {"net_amount": Decimal("17.231")},
            }[fault]
        )
    else:
        revision = Record(
            id=uuid4(),
            group_version=2,
            credit_id=credit.id,
            ledger_id=credit.ledger_id,
            document_id=document.id,
            action="cancel",
            credit_date=None,
            credit_reason=None,
            revision_reason="Fictional cancellation",
        )
        fact.update(group_version=2, revision_id=revision.id, credit_date=None, credit_reason=None)
        if fault == "wrong_revision_scope":
            revision.document_id = uuid4()
    with pytest.raises(LedgerError) as failure:
        _current(credit, document, ASSETS["USD"], revision, facts)
    assert (failure.value.code, failure.value.status) == ("credit_revision_integrity", 503)


def test_query_compiles_with_bounded_latest_group_and_no_write_lock(monkeypatch):
    credit, document, fact = data()
    record = Record(
        code="USD", kind="fiat", scale=2, network=None, token_reference=None, enabled=True
    )
    statements = []

    def execute(statement):
        statements.append(statement.compile(dialect=postgresql.dialect()))
        return (
            Record(one_or_none=lambda: (credit, document, record, None))
            if len(statements) == 1
            else Record(mappings=lambda: Record(all=lambda: [fact]))
        )

    reader = BusinessCreditRevisionReads(None)
    monkeypatch.setattr(
        reader,
        "_transaction",
        lambda owner, *, read_only: (
            nullcontext(Record(execute=execute)) if read_only else pytest.fail("Not read-only")
        ),
    )
    monkeypatch.setattr(reader, "_ledger", lambda *args: None)
    assert reader.current(OWNER, credit.ledger_id, credit.id).version == 1
    sql = [str(statement) for statement in statements]
    assert "LEFT OUTER JOIN LATERAL" in sql[0] and "DESC" in sql[0]
    assert "public.coinpup_credit_posting_facts" in sql[1]
    assert [credit.id] in statements[1].params.values() and 201 in statements[1].params.values()
    assert all("FOR UPDATE" not in query and "journal_lines" not in query for query in sql)


def test_current_route_requires_session_and_never_returns_a_receipt(client):
    response = client.get(PATH)
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"


def test_current_route_passes_authorized_identity_and_exact_path(signed_in, monkeypatch):
    credit, document, fact = data()
    current = _current(credit, document, ASSETS["USD"], None, [fact])
    calls = []

    def capture(self, owner, ledger, identifier):
        calls.append((owner, ledger, identifier))
        return current

    monkeypatch.setattr(BusinessCreditRevisionReads, "current", capture)
    response = signed_in.get(PATH)
    assert response.status_code == 200 and response.json() == current.model_dump(mode="json")
    assert "receipt" not in response.json() and response.headers["cache-control"] == "no-store"
    assert calls == [(OWNER, UUID(RECORD), UUID(RECORD))]
    assert signed_in.get(PATH.replace(RECORD + "/current", "invalid/current")).status_code == 422
    assert len(calls) == 1


@pytest.mark.parametrize("storage", [False, True])
def test_current_read_failures_keep_private_storage_details_hidden(signed_in, monkeypatch, storage):
    def fail(*args):
        raise (
            SQLAlchemyError("fictional private SQL")
            if storage
            else LedgerError("not_found", 404, "The record was not found.")
        )

    monkeypatch.setattr(BusinessCreditRevisionReads, "current", fail)
    response = signed_in.get(PATH)
    assert response.status_code == (503 if storage else 404)
    assert response.headers["cache-control"] == "no-store" and "private SQL" not in response.text
