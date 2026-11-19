"""New fictional credits bind the effective document while every original receipt stays fixed."""

from decimal import Decimal
from uuid import uuid4

import pytest
from coinpup_api.business.credit_revision_schemas import CreditCancellationCreate
from coinpup_api.business.credit_revisions import BusinessCreditRevisionService
from coinpup_api.business.credit_schemas import BusinessCreditCreate
from coinpup_api.business.document_revision_schemas import ConfirmedDocumentCancellation
from coinpup_api.business.document_revision_versions import read_confirmed_document_version
from coinpup_api.ledger.models import JournalLine
from coinpup_api.ledger.schemas import AssetUpdate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from tests.integration.business.test_credit_revision_boundaries import race
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_document_revision_capacity import replacement
from tests.integration.business.test_document_revision_commands import correction, send
from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_document_revision_storage import reviewed
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def current(s, doc):
    with Session(s["engine"]) as db:
        return read_confirmed_document_version(db, s["ledger"], doc.id)


def request(doc, amount="0.01", *, sources=None):
    return BusinessCreditCreate(
        id=uuid4(),
        document_id=doc.id,
        expected_version=doc.version,
        credit_date="2026-11-06",
        reason="Fictional current-version credit 中文",
        lines=[
            dict(
                source_line_id=row.id,
                net_amount=amount,
                tax_amount="0",
                recognition_date="2026-11-06",
            )
            for row in (sources or doc.lines)
        ],
    )


@pytest.mark.parametrize("kind", ["invoice", "bill"])
@pytest.mark.parametrize("new_ids,count", [(False, 1), (False, 200), (True, 200)])
def test_complete_current_sources_keep_original_documents_and_permanent_credit_receipt(
    drafts, kind, new_ids, count
):
    s = drafts
    doc, old_postings = confirmed(s, kind, count)
    rows = replacement(doc).lines
    if new_ids:
        rows = [row.model_copy(update={"id": uuid4()}) for row in reversed(rows)]
    command = correction(doc).model_copy(update={"replacement": reviewed(doc, rows)})
    fixed = send(s, doc, command, "fictional-credit-source-change")
    active = current(s, doc)
    payload = request(active.document)
    result = credit(s, payload)
    assert result.asset_id == "USD" and result.source_version == doc.version + 1
    assert len(result.postings) == count
    assert [p.source_line_id for p in result.postings] == [row.id for row in rows]
    expected = {p.source_line_id: p for p in active.postings}
    assert all(
        p.source_operation_id == expected[p.source_line_id].operation_id for p in result.postings
    )
    assert {p.version for p in active.postings} == ({1} if new_ids else {2})
    with Session(s["engine"]) as db:
        origin = read_confirmed_document_version(db, s["ledger"], doc.id, version=doc.version)
        assert origin.document == doc and origin.postings == old_postings
        assert db.execute(
            select(JournalLine.asset_id, func.sum(JournalLine.amount)).group_by(
                JournalLine.asset_id
            )
        ).all() == [("USD", Decimal(0))]
    cancelled = BusinessCreditRevisionService(s["engine"]).cancel(
        s["owner"],
        s["ledger"],
        result.id,
        CreditCancellationCreate(expected_version=1, reason="Fictional explicit release"),
        "fictional-current-credit-cancel",
    )
    assert cancelled.version == 2
    send(
        s,
        doc,
        ConfirmedDocumentCancellation(
            expected_version=fixed.version, reason="Fictional document end"
        ),
        "fictional-current-document-end",
    )
    change_period(s, cutoff="2026-11-06")
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    assert credit(s, payload) == result
    assert send(s, doc, command, "fictional-credit-source-change") == fixed
    assert snapshot(s) == before


@pytest.mark.parametrize("asset,unit", [("BTC", "0.00000001"), ("ETH", "0.000000000000000001")])
@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_new_credit_uses_replacement_asset_minimum_unit_without_changing_usd_evidence(
    drafts, asset, unit, kind
):
    s = drafts
    doc, _ = confirmed(s, kind, 1)
    command = correction(doc).model_copy(
        update={
            "replacement": replacement(doc, {doc.lines[0].id: {"unit_price": unit}}, asset_id=asset)
        }
    )
    send(s, doc, command, "fictional-asset-change")
    payload = request(current(s, doc).document, unit)
    fixed = credit(s, payload)
    assert fixed.asset_id == asset
    assert Decimal(fixed.postings[0].total_amount) == Decimal(unit)
    with Session(s["engine"]) as db:
        totals = dict(
            db.execute(
                select(JournalLine.asset_id, func.sum(JournalLine.amount)).group_by(
                    JournalLine.asset_id
                )
            ).all()
        )
        assert totals == {"USD": Decimal(0), asset: Decimal(0)}
    before = snapshot(s)
    assert credit(s, payload) == fixed
    assert snapshot(s) == before


def test_removed_and_zero_sources_reject_while_zero_to_positive_gets_a_new_accrual(drafts):
    s = drafts
    doc, old = confirmed(s, "invoice", 4)
    proposed = replacement(
        doc,
        {
            doc.lines[0].id: None,
            doc.lines[1].id: {"unit_price": "0"},
            doc.lines[2].id: {"unit_price": "0.01"},
        },
    )
    send(s, doc, correction(doc).model_copy(update={"replacement": proposed}), "fictional-zero")
    active = current(s, doc)
    for source in (doc.lines[0], doc.lines[1], doc.lines[3]):
        before = snapshot(s)
        with pytest.raises(LedgerError) as rejected:
            credit(s, request(active.document, sources=[source]))
        assert rejected.value.code == "not_found" and snapshot(s) == before
    fixed = credit(s, request(active.document, sources=[doc.lines[2]]))
    assert fixed.postings[0].source_operation_id not in {p.operation_id for p in old}


@pytest.mark.parametrize(
    "fault,code",
    [
        ("stale", "version_conflict"),
        ("precision", "amount_precision"),
        ("capacity", "credit_exceeds_source"),
        ("closed", "period_closed"),
        ("terminal", "document_cancelled"),
    ],
)
def test_invalid_new_current_intents_preserve_every_table(drafts, fault, code):
    s = drafts
    doc, _ = confirmed(s, "invoice", 1)
    send(s, doc, correction(doc), "fictional-invalid-source-change")
    active = current(s, doc)
    payload = request(active.document)
    if fault == "stale":
        payload = payload.model_copy(update={"expected_version": doc.version})
    if fault in {"precision", "capacity"}:
        payload = request(active.document, "0.001" if fault == "precision" else "0.02")
    if fault == "closed":
        change_period(s, cutoff="2026-11-06")
    if fault == "terminal":
        fixed = send(
            s,
            doc,
            ConfirmedDocumentCancellation(
                expected_version=active.version, reason="Fictional terminal source"
            ),
            "fictional-terminal-source",
        )
        payload = payload.model_copy(update={"expected_version": fixed.version})
    before = snapshot(s)
    with pytest.raises(LedgerError) as rejected:
        credit(s, payload)
    assert rejected.value.code == code and snapshot(s) == before


@pytest.mark.parametrize("same_key", [True, False])
def test_two_actual_transactions_serialize_current_source_capacity(drafts, same_key):
    s = drafts
    doc, _ = confirmed(s, "invoice", 1)
    send(s, doc, correction(doc), "fictional-racing-source-change")
    payload = request(current(s, doc).document)
    results = race(
        s,
        [
            lambda n=n: credit(
                s,
                payload if same_key else payload.model_copy(update={"id": uuid4()}),
                "fictional-same-credit" if same_key else f"fictional-credit-{n}",
            )
            for n in (0, 1)
        ],
    )
    successful = [r for r in results if not isinstance(r, LedgerError)]
    assert len(successful) == (2 if same_key else 1)
    if same_key:
        assert successful[0] == successful[1]
    else:
        assert [r.code for r in results if isinstance(r, LedgerError)] == ["credit_exceeds_source"]


def test_failed_new_credit_receipt_rolls_back_all_current_source_writes_and_can_retry(drafts):
    s = drafts
    doc, _ = confirmed(s, "invoice", 1)
    send(s, doc, correction(doc), "fictional-atomic-source-change")
    payload = request(current(s, doc).document)
    before = snapshot(s)

    def fail(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().startswith("INSERT INTO command_receipts "):
            raise RuntimeError("Fictional failure after receipt insert")

    event.listen(s["engine"], "after_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="Fictional failure after receipt insert"):
            credit(s, payload)
    finally:
        event.remove(s["engine"], "after_cursor_execute", fail)
    assert snapshot(s) == before
    fixed = credit(s, payload)
    before = snapshot(s)
    assert credit(s, payload) == fixed and snapshot(s) == before
