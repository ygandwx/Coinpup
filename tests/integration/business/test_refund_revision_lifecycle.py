"""Real commits verify a single operation, explicit evidence, immutable receipts and reversals."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.refund_revision_models import BusinessRefundRevision as Revision
from coinpup_api.business.refund_revision_return_models import (
    BusinessRefundRevisionReturn as Evidence,
)
from coinpup_api.business.refund_revision_schemas import RefundRevisionResponse
from coinpup_api.business.refund_schemas import BusinessRefundCreate
from coinpup_api.business.settlement_totals import payment_totals
from coinpup_api.ledger.models import FinancialOperation, Journal, JournalLine
from coinpup_api.sync.locking import acquire_write_lock
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_credit_fact_validation import copy_posting, stored
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_posting_fact import fact
from tests.integration.business.test_refund_revision_receipt import add_receipt, prove, response
from tests.integration.business.test_refund_service import refund, setup_refund
from tests.integration.conftest import refund_lifecycle_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ledger.test_posting_service_database import opening

pytestmark = pytest.mark.integration


def advance(c, s, original, action, *, references=(), principal=None, problem=None):
    acquire_write_lock(c)
    operation = stored(c, FinancialOperation, original.operation_id)
    previous = operation["current_journal_id"]
    old = fact(c, s["ledger"], previous)
    version = operation["version"] + 1
    revise = action == "correct"
    row = dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        created_by=s["owner"],
        refund_id=original.id,
        payment_id=original.payment_id,
        operation_id=original.operation_id,
        operation_version=version,
        previous_revision_id=c.scalar(
            select(Revision.id).where(
                Revision.refund_id == original.id, Revision.operation_version == version - 1
            )
        ),
        previous_journal_id=previous,
        reversal_journal_id=uuid4(),
        action=action,
        revision_reason=f"Fictional refund revision {version}",
        idempotency_key=f"fictional-change-{version}",
    )
    if revise:
        row.update(
            replacement_journal_id=uuid4(),
            account_id=old["account_id"],
            asset_id=old["asset_id"],
            amount=old["amount"] if principal is None else principal,
            payment_amount=old["payment_amount"] if principal is None else principal,
            transaction_date=old["transaction_date"] + timedelta(days=1),
            reason="Fictional replacement facts",
            fees=old["fees"],
        )
    c.execute(insert(Revision).values(**row))
    for position, returned in enumerate(references, 1):
        c.execute(
            insert(Evidence).values(
                id=uuid4(),
                revision_id=row["id"],
                refund_id=original.id,
                payment_id=original.payment_id,
                ledger_id=s["ledger"],
                return_id=returned,
                return_version=1,
                line_no=position,
            )
        )
    c.execute(
        update(FinancialOperation)
        .where(FinancialOperation.id == original.operation_id)
        .values(
            version=version,
            status="active" if revise else "cancelled",
            current_journal_id=row["replacement_journal_id"] if revise else previous,
        )
    )
    if problem != "missing_reversal":
        copy_posting(
            c,
            previous,
            dict(
                id=row["reversal_journal_id"],
                operation_version=version,
                journal_kind="reversal",
                reverses_journal_id=previous,
                revision_reason=row["revision_reason"],
            ),
            reverse=problem != "unreversed",
        )
    if revise:
        posting = dict(
            id=row["replacement_journal_id"],
            operation_version=version,
            transaction_date=row["transaction_date"],
            recognition_date=row["transaction_date"],
            description=row["reason"],
            revision_reason=row["revision_reason"],
        )
        if principal is None:
            copy_posting(c, previous, posting)
        else:
            # This direct fixture changes only the two principal legs of a same-asset refund.
            c.execute(
                insert(Journal).values(
                    **(stored(c, Journal, previous) | dict(sealed=False) | posting)
                )
            )
            for line in (
                c.execute(select(JournalLine.__table__).where(JournalLine.journal_id == previous))
                .mappings()
                .all()
            ):
                copied = dict(line) | dict(id=uuid4(), journal_id=posting["id"])
                if copied["component_no"] == 0:
                    assert (
                        old["asset_id"] == old["payment_asset_id"]
                        and abs(line["amount"]) == old["amount"]
                    )
                    copied["amount"] = principal.copy_sign(line["amount"])
                c.execute(insert(JournalLine).values(**copied))
    expected = response(c, row["id"])
    if problem != "missing_receipt":
        add_receipt(
            c, row, expected if problem != "wrong_receipt" else expected | {"version": version + 1}
        )
    return row, expected


@pytest.mark.parametrize(
    "direction,asset,amount", [("payout", "USD", "20"), ("receipt", "EUR", "19.37")]
)
def test_complete_history_commits_and_keeps_every_fixed_receipt_and_source(
    drafts, direction, asset, amount
):
    s = drafts
    _, _, body = setup_refund(s, direction, asset, amount)
    s["posting"].post_opening(
        s["owner"], s["ledger"], opening(s, "1", "BTC"), "fictional-lifecycle-fees"
    )
    body = BusinessRefundCreate.model_validate(
        body.model_dump()
        | dict(
            fees=[
                dict(
                    account_id=body.account_id,
                    category_id=s["expenses"][0].id,
                    asset_id="BTC",
                    amount="0.00000001",
                )
            ]
        )
    )
    original = refund(s, body)
    first_evidence = tuple(original.allocation_return_ids)
    assert first_evidence
    with s["engine"].begin() as c:
        first, one = advance(c, s, original, "correct", references=())
    with s["engine"].begin() as c:
        second, two = advance(c, s, original, "correct", references=first_evidence)
    with s["engine"].begin() as c:
        last, three = advance(c, s, original, "cancel")
    before = snapshot(s)
    with s["engine"].connect() as c:
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        op = stored(c, FinancialOperation, original.operation_id)
        assert (op["status"], op["version"], op["current_journal_id"]) == (
            "cancelled",
            4,
            second["replacement_journal_id"],
        )
        for row, receipt in ((first, one), (second, two), (last, three)):
            assert response(c, row["id"]) == receipt
            assert (
                RefundRevisionResponse.model_validate(receipt).version == row["operation_version"]
            )
            prove(c, row["id"])
        assert one["previous"]["allocation_return_ids"] == list(map(str, first_evidence))
        assert one["replacement"]["allocation_return_ids"] == []
        assert two["previous"] == one["replacement"]
        assert three["previous"] == two["replacement"] and three["replacement"] is None
        assert (
            two["replacement"]["allocation_return_ids"] == one["previous"]["allocation_return_ids"]
        )
        assert two["replacement"]["fees"] == one["previous"]["fees"]
        assert (
            c.execute(select(Journal.sealed).where(Journal.operation_id == original.operation_id))
            .scalars()
            .all()
            == [True] * 6
        )
        assert c.execute(
            select(ChangeLog.entity_version, ChangeLog.change_kind)
            .where(
                ChangeLog.entity_id == str(original.id), ChangeLog.entity_type == "business_refunds"
            )
            .order_by(ChangeLog.seq)
        ).all() == [(1, "upsert"), (2, "upsert"), (3, "upsert"), (4, "cancel")]
        assert (
            c.scalar(
                text(
                    "SELECT count(*) FROM coinpup_current_refund_principals("
                    ":ledger,ARRAY[CAST(:payment AS uuid)])"
                ),
                {"ledger": s["ledger"], "payment": original.payment_id},
            )
            == 0
        )
    assert refund(s, body) == original and snapshot(s) == before


@pytest.mark.parametrize(
    "problem,constraint",
    [
        ("missing_reversal", "ck_refund_revision_shape"),
        ("unreversed", "ck_journal_reversal"),
        ("missing_receipt", "ck_refund_revision_shape"),
        ("wrong_receipt", "ck_refund_revision_shape"),
        ("over_capacity", "ck_business_refund_shape"),
    ],
)
def test_incomplete_or_excess_direct_revisions_rollback_the_entire_group(
    drafts, problem, constraint
):
    s = drafts
    _, _, body = setup_refund(s, allocated=True)
    original = refund(s, body)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
        advance(
            c,
            s,
            original,
            "correct",
            principal=Decimal("20.01") if problem == "over_capacity" else None,
            problem=problem,
        )
        c.exec_driver_sql("SET CONSTRAINTS tr_business_refund_revisions_complete IMMEDIATE")
    assert rejected.value.orig.sqlstate == "23514"
    assert rejected.value.orig.diag.constraint_name == constraint
    assert refund(s, body) == original and snapshot(s) == before


def test_evidence_appended_later_cannot_change_an_already_receipted_correction(drafts):
    s = drafts
    _, _, body = setup_refund(s)
    original = refund(s, body)
    with s["engine"].begin() as c:
        row, fixed = advance(c, s, original, "correct")
    before = snapshot(s)
    with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
        c.execute(
            insert(Evidence).values(
                id=uuid4(),
                ledger_id=s["ledger"],
                refund_id=original.id,
                payment_id=original.payment_id,
                revision_id=row["id"],
                line_no=1,
                return_version=1,
                return_id=original.allocation_return_ids[0],
            )
        )
    assert rejected.value.orig.diag.constraint_name == "ck_refund_revision_shape"
    with s["engine"].connect() as c:
        assert response(c, row["id"]) == fixed
    assert snapshot(s) == before


def test_reduced_current_principal_can_be_used_once_then_blocks_excess_correction(drafts):
    s = drafts
    _, _, body = setup_refund(s)
    original = refund(s, body)
    with s["engine"].begin() as c:
        advance(c, s, original, "correct", principal=Decimal("0.01"))
    other_body = body.model_copy(
        update=dict(id=uuid4(), amount="19.99", payment_amount="19.99", allocation_return_ids=[])
    )
    other = refund(s, other_body, key="fictional-released-principal")
    before = snapshot(s)
    with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
        advance(c, s, original, "correct", principal=Decimal("0.02"))
        c.exec_driver_sql("SET CONSTRAINTS tr_business_refund_revisions_complete IMMEDIATE")
    assert rejected.value.orig.diag.constraint_name == "ck_business_refund_shape"
    assert snapshot(s) == before
    with s["engine"].begin() as c:
        advance(c, s, original, "cancel")
    with s["engine"].connect() as c:
        assert payment_totals(c, s["ledger"], [original.payment_id])[original.payment_id] == (
            Decimal("80"),
            Decimal("19.99"),
        )
    assert refund(s, other_body, key="fictional-released-principal") == other


def test_cancelled_refund_never_accepts_a_new_revision_row(drafts):
    s = drafts
    _, _, body = setup_refund(s, allocated=False)
    original = refund(s, body)
    with s["engine"].begin() as c:
        prior, _ = advance(c, s, original, "cancel")
    before = snapshot(s)
    with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
        c.execute(
            insert(Revision).values(
                **(
                    prior
                    | dict(
                        id=uuid4(),
                        operation_version=3,
                        previous_revision_id=prior["id"],
                        previous_journal_id=original.journal_id,
                        reversal_journal_id=uuid4(),
                        idempotency_key="fictional-after-cancel",
                    )
                )
            )
        )
        c.exec_driver_sql("SET CONSTRAINTS tr_business_refund_revisions_complete IMMEDIATE")
    assert rejected.value.orig.diag.constraint_name == "ck_refund_revision_shape"
    assert refund(s, body) == original and snapshot(s) == before


def test_isolated_refund_head_with_exact_reversal_still_needs_a_revision_and_receipt(drafts):
    s = drafts
    _, _, body = setup_refund(s, allocated=False)
    original = refund(s, body)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
        acquire_write_lock(c)
        c.execute(
            update(FinancialOperation)
            .where(FinancialOperation.id == original.operation_id)
            .values(version=2, status="cancelled")
        )
        copy_posting(
            c,
            original.journal_id,
            dict(
                id=uuid4(),
                operation_version=2,
                journal_kind="reversal",
                reverses_journal_id=original.journal_id,
                revision_reason="Fictional isolated cancellation",
            ),
            reverse=True,
        )
        c.exec_driver_sql("SET CONSTRAINTS zz_tr_refund_operations_complete IMMEDIATE")
    assert rejected.value.orig.diag.constraint_name == "ck_business_refund_shape"
    assert snapshot(s) == before


def test_lifecycle_downgrade_requires_empty_revision_history_and_restores_previous_definitions(
    drafts,
):
    s = drafts
    _, _, body = setup_refund(s, allocated=False)
    original = refund(s, body)
    migration = refund_lifecycle_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        live = definitions(c)
        migration["downgrade"]()
        assert definitions(c) != live
        c.execute(text("SELECT coinpup_validate_business_refund(:id)"), {"id": original.id})
        migration["upgrade"]()
        assert definitions(c) == live
    with s["engine"].begin() as c:
        advance(c, s, original, "cancel")
    before = snapshot(s)
    with (
        s["engine"].connect() as c,
        Operations.context(MigrationContext.configure(c)),
        pytest.raises(IntegrityError) as rejected,
    ):
        migration["downgrade"]()
    assert rejected.value.orig.diag.constraint_name == "ck_refund_revision_downgrade"
    assert refund(s, body) == original and snapshot(s) == before
