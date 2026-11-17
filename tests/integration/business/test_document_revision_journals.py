"""Fictional sealed invoice/bill accruals are completely reversed in their actual original units."""

from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest
from coinpup_api.business import confirmations
from coinpup_api.business.confirmation_reads import BusinessConfirmationReads
from coinpup_api.business.confirmation_schemas import BusinessConfirmationCreate
from coinpup_api.business.document_revision_journals import prepare_document_revision_journals
from coinpup_api.business.document_revision_plan import SealedAccrual
from coinpup_api.business.document_revision_schemas import (
    ConfirmedDocumentCancellation,
    ConfirmedDocumentCorrection,
)
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import Account, Journal, JournalLine
from coinpup_api.ledger.posting_storage import DIMENSION_FIELDS
from coinpup_api.ledger.schemas import AssetUpdate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from tests.integration.business.test_confirmation_service import submit
from tests.integration.business.test_document_revision_capacity import replacement
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def confirmed(s, kind, count=4):
    raw = s["payload"](document_kind=kind)
    master = raw.lines[0].model_copy(
        update={
            "quantity": "1",
            "unit_price": "0.01",
            "discount_amount": "0",
            "tax_rate_percent": "0",
            "category_id": s["income"].id if kind == "invoice" else s["expenses"][0].id,
        }
    )
    rows = [
        master.model_copy(
            update={"id": uuid4(), "unit_price": "0" if count == 4 and i > 1 else "0.01"}
        )
        for i in range(count)
    ]
    saved = s["service"].create_draft(
        s["owner"], s["ledger"], raw.model_copy(update={"lines": rows})
    )
    receipt = submit(
        s, saved, BusinessConfirmationCreate(id=uuid4(), expected_version=saved.version)
    )
    doc = (
        BusinessConfirmationReads(s["engine"])
        .get_confirmation(s["owner"], s["ledger"], saved.id)
        .document
    )
    postings = tuple(
        SealedAccrual(s["ledger"], doc.id, p.line_id, p.operation_id, 1, p.journal_id)
        for p in receipt.postings
    )
    return doc, postings


def cancellation(doc):
    return ConfirmedDocumentCancellation(
        expected_version=doc.version, reason="Fictional cancellation"
    )


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_reverse_preexisting_ledger_line_numbers_and_roles(drafts, monkeypatch, kind):
    # Historical SQL accepts both role orders and positive, nonconsecutive line numbers.
    # Insert those facts before the real original confirmation seals its journals.
    original = confirmations.accrual_posting_lines

    def historical_rows(*args):
        rows, _ = original(*args)
        account, category = rows
        rows = [replace(category, line_no=4), replace(account, line_no=9)]
        return rows, confirmations.prepare_journal_lines(rows, {"USD": ASSETS["USD"]})

    with monkeypatch.context() as patch:
        patch.setattr(confirmations, "accrual_posting_lines", historical_rows)
        doc, postings = confirmed(drafts, kind, count=1)

    before = snapshot(drafts)
    with Session(drafts["engine"]) as db:
        original_rows = db.scalars(
            select(JournalLine)
            .where(JournalLine.journal_id == postings[0].journal_id)
            .order_by(JournalLine.line_no)
        ).all()
        assert [r.line_no for r in original_rows] == [4, 9]
        plan = prepare_document_revision_journals(
            db, doc, cancellation(doc), postings, ASSETS["USD"], None
        )
        reverted = plan.members[0].reversal
        assert reverted.header["reverses_journal_id"] == postings[0].journal_id
        for old, new in zip(original_rows, reverted.parameters, strict=True):
            assert new["amount"] == -old.amount
            for field in (
                "line_no",
                "component_no",
                "role",
                "asset_id",
                "account_id",
                "category_id",
                *DIMENSION_FIELDS,
            ):
                assert new.get(field) == getattr(old, field)
    assert snapshot(drafts) == before


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_assembled_journals_copy_every_old_dimension_and_reprice_new_asset(drafts, kind):
    s = drafts
    doc, postings = confirmed(s, kind)
    key = "receivable.customer" if kind == "invoice" else "payable.supplier"
    with Session(s["engine"]) as db, db.begin():
        ControlAccounts(s["engine"]).ensure(db, s["owner"], s["ledger"], {key: {"BTC"}})
    new_day = date(2026, 11, 2)
    spec = replacement(
        doc,
        {
            doc.lines[1].id: None,
            doc.lines[0].id: {"unit_price": "0.00000002"},
            doc.lines[2].id: {"unit_price": "0.00000003"},
        },
        asset_id="BTC",
        issue_date=new_day,
    )
    added = spec.lines[0].model_copy(
        update={"id": uuid4(), "unit_price": "0.00000001", "recognition_date": new_day}
    )
    spec = spec.model_copy(update={"lines": [*spec.lines, added]})
    cmd = ConfirmedDocumentCorrection(
        expected_version=doc.version, reason="Fictional replacement", replacement=spec
    )
    before = snapshot(s)
    with Session(s["engine"]) as db:
        plan = prepare_document_revision_journals(
            db, doc, cmd, postings, ASSETS["USD"], ASSETS["BTC"]
        )
        parts = {m.change.source_line_id: m for m in plan.members}
        assert set(plan.affected_dates) == {
            doc.issue_date,
            new_day,
            *(r.recognition_date for r in doc.lines),
        }
        assert [(part.change.posting_action, part.operation_version) for part in plan.members] == [
            ("correct", 2),
            ("create", 1),
            (None, None),
            ("create", 1),
            ("cancel", 2),
        ]
        assert {p.operation_id for p in postings} == {
            parts[p.source_line_id].operation_id for p in postings
        }
        assert len({part.operation_id for part in plan.members if part.operation_id}) == 4
        for prior in postings:
            member = parts[prior.source_line_id]
            old = db.get(Journal, prior.journal_id)
            assert (
                member.reversal.header["transaction_date"],
                member.reversal.header["recognition_date"],
                member.reversal.header["description"],
                member.reversal.header["reverses_journal_id"],
            ) == (old.transaction_date, old.recognition_date, old.description, old.id)
            original = db.scalars(
                select(JournalLine)
                .where(JournalLine.journal_id == prior.journal_id)
                .order_by(JournalLine.line_no)
            ).all()
            for old_line, new in zip(original, member.reversal.parameters, strict=True):
                assert new["amount"] == -old_line.amount
                for field in (
                    "ledger_id",
                    "asset_id",
                    "line_no",
                    "component_no",
                    "role",
                    "account_id",
                    "category_id",
                    *DIMENSION_FIELDS,
                ):
                    assert new[field] == getattr(old_line, field)
        control = db.scalar(
            select(Account.id).where(Account.ledger_id == doc.ledger_id, Account.system_key == key)
        )
        for member in plan.members:
            posted = member.replacement
            if posted is None:
                continue
            source = member.change.replacement
            assert posted.header["transaction_date"] == new_day
            assert posted.header["recognition_date"] == source.recognition_date
            assert posted.header["revision_reason"] == (
                cmd.reason if member.operation_version == 2 else None
            )
            first, second = posted.lines
            assert first.account_id == control and second.category_id == source.category_id
            assert first.amount.minor_units == -second.amount.minor_units
            assert (
                first.amount.minor_units
                == (1 if kind == "invoice" else -1) * member.change.accrual.price.total.minor_units
            )
            assert all(
                row.asset_id == "BTC"
                and row.document_line_id == source.id
                and row.party_id == spec.party_id
                for row in posted.lines
            )
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "fault",
    [
        "foreign-journal",
        "wrong-operation",
        "wrong-original-date",
        "wrong-source-description",
        "wrong-amount",
        "stale-command",
        "unacquired-new-control",
    ],
)
def test_mismatched_sealed_sources_are_rejected_before_any_write(drafts, fault):
    s = drafts
    doc, postings = confirmed(s, "bill")
    cmd = cancellation(doc)
    new = None
    if fault == "foreign-journal":
        postings = (replace(postings[0], journal_id=uuid4()), *postings[1:])
    elif fault == "wrong-operation":
        postings = (replace(postings[0], operation_id=uuid4()), *postings[1:])
    elif fault == "wrong-original-date":
        doc = doc.model_copy(update={"issue_date": date(2026, 1, 1)})
    elif fault == "wrong-source-description":
        doc = doc.model_copy(
            update={
                "lines": [
                    doc.lines[0].model_copy(update={"description": "Fictional wrong"}),
                    *doc.lines[1:],
                ]
            }
        )
    elif fault == "wrong-amount":
        doc = doc.model_copy(update={"total_amount": "2"})
    elif fault == "stale-command":
        cmd = cmd.model_copy(update={"expected_version": doc.version + 1})
    elif fault == "unacquired-new-control":
        new = ASSETS["BTC"]
        cmd = ConfirmedDocumentCorrection(
            expected_version=doc.version,
            reason=cmd.reason,
            replacement=replacement(doc, asset_id="BTC"),
        )
    before = snapshot(s)
    with pytest.raises((MoneyError, LedgerError)) as err, Session(s["engine"]) as db:
        prepare_document_revision_journals(db, doc, cmd, postings, ASSETS["USD"], new)
    assert err.value.code == (
        "version_conflict" if fault == "stale-command" else "document_revision_integrity"
    )
    assert snapshot(s) == before


def test_all_two_hundred_sealed_accruals_can_reverse_disabled_asset_in_one_read(drafts):
    s = drafts
    doc, postings = confirmed(s, "invoice", 200)
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    before = snapshot(s)
    with Session(s["engine"]) as db:
        conn = db.connection()
        selects = []

        def count(_conn, _cursor, statement, *args):
            if statement.lstrip().upper().startswith("SELECT"):
                selects.append(statement)

        event.listen(conn, "before_cursor_execute", count)
        plan = prepare_document_revision_journals(
            db, doc, cancellation(doc), postings, replace(ASSETS["USD"], enabled=False), None
        )
        assert len(selects) == 1
        assert len(plan.members) == 200
        assert sum(len(m.reversal.parameters) for m in plan.members) == 400
        assert all(
            m.operation_version == 2
            and m.replacement is None
            and m.reversal.header["operation_id"] == m.operation_id
            for m in plan.members
        )
    assert snapshot(s) == before


def test_two_hundred_complete_source_swaps_prepare_four_hundred_distinct_movements(drafts):
    s = drafts
    doc, postings = confirmed(s, "bill", 200)
    reviewed = replacement(doc)
    changed = reviewed.model_copy(
        update={
            "lines": [row.model_copy(update={"id": uuid4()}) for row in reversed(reviewed.lines)]
        }
    )
    cmd = ConfirmedDocumentCorrection(
        expected_version=doc.version, reason="Fictional total replacement", replacement=changed
    )
    before = snapshot(s)
    with Session(s["engine"]) as db:
        result = prepare_document_revision_journals(
            db, doc, cmd, postings, ASSETS["USD"], ASSETS["USD"]
        )
        created, removed = result.members[:200], result.members[200:]
        assert [m.change.source_line_id for m in created] == [row.id for row in changed.lines]
        assert [m.change.source_line_id for m in removed] == [row.id for row in doc.lines]
        assert len({m.operation_id for m in result.members}) == 400
        assert all(
            m.reversal is None and m.operation_version == 1 and len(m.replacement.parameters) == 2
            for m in created
        )
        assert all(
            m.replacement is None and m.operation_version == 2 and len(m.reversal.parameters) == 2
            for m in removed
        )
        assert {m.reversal.header["reverses_journal_id"] for m in removed} == {
            p.journal_id for p in postings
        }
        assert (
            len(
                {
                    j.header["id"]
                    for m in result.members
                    for j in (m.replacement, m.reversal)
                    if j is not None
                }
            )
            == 400
        )
    assert snapshot(s) == before
