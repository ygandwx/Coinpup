"""Refund evidence binds the exact observed return version and keeps its historical date."""

from datetime import date
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.allocation_return_revision_models import (
    BusinessAllocationReturnRevision as ReturnRevision,
)
from coinpup_api.business.refund_models import BusinessRefund, BusinessRefundReturn
from coinpup_api.business.refund_revision_models import BusinessRefundRevision as RefundRevision
from coinpup_api.business.refund_revision_return_models import BusinessRefundRevisionReturn
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_credit_fact_validation import stored
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_headers import replacement
from tests.integration.business.test_refund_service import refund
from tests.integration.business.test_return_revision_refund_guard import example
from tests.integration.conftest import bound_refund_evidence_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def current(c, ledger, payment, ids):
    return c.execute(
        text("SELECT * FROM coinpup_current_return_evidence(:l,:p,CAST(:ids AS uuid[]))"),
        {"l": ledger, "p": payment, "ids": ids},
    ).all()


def historical(c, ledger, payment, return_id, version):
    return c.scalar(
        text("SELECT coinpup_bound_return_date(:l,:p,:id,:v)"),
        {"l": ledger, "p": payment, "id": return_id, "v": version},
    )


def revise_return(c, s, group, on=date(2026, 10, 2), previous=None):
    h = dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        return_id=group.id,
        payment_id=group.payment_id,
        created_by=s["owner"],
        idempotency_key="fictional-bound-return-" + str(uuid4()),
        group_version=2 if previous is None else previous["group_version"] + 1,
        previous_revision_id=None if previous is None else previous["id"],
        action="correct" if on else "cancel",
        revision_reason="Fictional change observed evidence",
        return_date=on,
        return_reason="Fictional new evidence date" if on else None,
    )
    c.execute(insert(ReturnRevision).values(**h))
    return h


def evidence_row(c, s, returned, body, original, kind):
    link = dict(
        id=uuid4(),
        return_id=returned.id,
        ledger_id=s["ledger"],
        payment_id=original.payment_id,
        line_no=1,
    )
    if kind == "new-refund":
        h = stored(c, BusinessRefund, original.id) | dict(
            id=uuid4(),
            operation_id=uuid4(),
            journal_id=uuid4(),
            idempotency_key="fictional-bound-cash-" + str(uuid4()),
        )
        c.execute(insert(BusinessRefund).values(**h))
        return BusinessRefundReturn, link | dict(refund_id=h["id"])
    h = replacement(body) | dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        refund_id=original.id,
        payment_id=original.payment_id,
        created_by=s["owner"],
        operation_id=original.operation_id,
        operation_version=2,
        previous_journal_id=original.journal_id,
        reversal_journal_id=uuid4(),
        revision_reason="Fictional binding revision",
        idempotency_key="fictional-version-refund",
    )
    c.execute(insert(RefundRevision).values(**h))
    return BusinessRefundRevisionReturn, link | dict(refund_id=original.id, revision_id=h["id"])


def stage_explicit_unlink(c, s, body, refund_id):
    """Stage the next explicit evidence-free correction in an already rollback-only SQL probe."""
    original = stored(c, BusinessRefund, refund_id)
    latest = (
        c.execute(
            select(RefundRevision.__table__)
            .where(RefundRevision.ledger_id == s["ledger"], RefundRevision.refund_id == refund_id)
            .order_by(RefundRevision.operation_version.desc())
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    if latest is None:
        version, journal = 1, original["journal_id"]
    else:
        version, journal = latest["operation_version"], latest["replacement_journal_id"]
    source = latest if latest is not None else original
    h = replacement(body) | dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        payment_id=original["payment_id"],
        refund_id=refund_id,
        operation_id=original["operation_id"],
        operation_version=version + 1,
        previous_revision_id=None if latest is None else latest["id"],
        previous_journal_id=journal,
        reversal_journal_id=uuid4(),
        replacement_journal_id=uuid4(),
        created_by=s["owner"],
        revision_reason="Fictional explicit removal of evidence",
        idempotency_key="fictional-explicit-unlink-" + str(uuid4()),
    )
    for field in (
        "account_id",
        "asset_id",
        "amount",
        "payment_amount",
        "transaction_date",
        "reason",
        "fees",
    ):
        h[field] = source[field]
    c.execute(insert(RefundRevision).values(**h))


@pytest.mark.parametrize("kind", ["new-refund", "refund-correction"])
def test_evidence_pins_v2_and_v1_dates_after_a_later_correction_or_terminal_return(drafts, kind):
    s = drafts
    _, returned, body, original = example(s)
    before = snapshot(s)
    with s["engine"].connect() as c:
        ledger = s["ledger"]
        payment = returned.payment_id
        assert current(c, ledger, payment, [returned.id, returned.id]) == [
            (returned.id, 1, returned.return_date)
        ]
        h = revise_return(c, s, returned)
        assert current(c, ledger, payment, [returned.id]) == [(returned.id, 2, date(2026, 10, 2))]
        model, link = evidence_row(c, s, returned, body, original, kind)
        c.execute(insert(model).values(**(link | dict(return_version=2))))
        follow = revise_return(c, s, returned, date(2026, 10, 1), h)
        assert current(c, ledger, payment, [returned.id]) == [(returned.id, 3, date(2026, 10, 1))]
        assert c.scalar(select(model.return_version).where(model.id == link["id"])) == 2
        assert (
            c.scalar(
                select(BusinessRefundReturn.return_version).where(
                    BusinessRefundReturn.refund_id == original.id
                )
            )
            == 1
        )
        found = c.execute(
            text(
                "SELECT refund_id,return_version FROM coinpup_current_refund_return_references("
                ":l,:p,ARRAY[CAST(:r AS uuid)])"
            ),
            {"l": ledger, "p": payment, "r": returned.id},
        ).all()
        expected = {(link["refund_id"], 2)}
        if kind == "new-refund":
            expected.add((original.id, 1))
        assert set(found) == expected
        for refund_id, _ in expected:
            stage_explicit_unlink(c, s, body, refund_id)
        end = revise_return(c, s, returned, None, follow)
        assert current(c, ledger, payment, [returned.id]) == []
        assert [historical(c, ledger, payment, returned.id, v) for v in [1, 2, 3, 4, 99]] == [
            returned.return_date,
            date(2026, 10, 2),
            date(2026, 10, 1),
            None,
            None,
        ]
        assert end["group_version"] == 4
        c.rollback()
    assert refund(s, body) == original and snapshot(s) == before


@pytest.mark.parametrize("kind", ["new-refund", "refund-correction"])
@pytest.mark.parametrize("case", ["old", "invented", "cancelled"])
def test_old_future_or_terminal_evidence_cannot_be_attached(drafts, kind, case):
    s = drafts
    _, returned, body, original = example(s, linked=case != "cancelled")
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        revise_return(c, s, returned, None if case == "cancelled" else date(2026, 10, 2))
        model, link = evidence_row(c, s, returned, body, original, kind)
        c.execute(
            insert(model).values(
                **(link | dict(return_version={"old": 1, "invented": 3, "cancelled": 2}[case]))
            )
        )
    expected = (
        "ck_business_refund_return_source_version"
        if kind == "new-refund"
        else "ck_refund_revision_return_source_version"
    )
    assert caught.value.orig.diag.constraint_name == expected
    assert snapshot(s) == before


def test_scope_and_empty_legacy_migration_roundtrip_keep_true_raw_201_and_bound_v1(drafts):
    s = drafts
    _, returned, body, original = example(s)
    before = snapshot(s)
    migration = bound_refund_evidence_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        baseline = definitions(c)
        assert historical(c, uuid4(), returned.payment_id, returned.id, 1) is None
        assert historical(c, s["ledger"], uuid4(), returned.id, 1) is None
        assert current(c, s["ledger"], uuid4(), [returned.id]) == []
        assert current(c, s["ledger"], returned.payment_id, []) == []
        migration["downgrade"]()
        assert not c.scalar(
            text(
                "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='business_refund_returns' "
                "AND column_name='return_version')"
            )
        )
        migration["upgrade"]()
        assert definitions(c) == baseline
        assert (
            c.scalar(
                select(BusinessRefundReturn.return_version).where(
                    BusinessRefundReturn.refund_id == original.id
                )
            )
            == 1
        )
    with pytest.raises(IntegrityError) as err, s["engine"].begin() as c:
        revise_return(c, s, returned)
        with Operations.context(MigrationContext.configure(c)):
            migration["downgrade"]()
    assert err.value.orig.diag.constraint_name == "ck_return_revision_downgrade"
    assert refund(s, body) == original and snapshot(s) == before
