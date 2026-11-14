"""Permanent 200 evidence uses only the specified refund version and its immediate predecessor."""

from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.refund_revision_models import BusinessRefundRevision as Revision
from coinpup_api.business.refund_revision_return_models import (
    BusinessRefundRevisionReturn as Evidence,
)
from coinpup_api.business.refund_revision_schemas import (
    RefundRevisionResponse,
    validate_refund_revision_amounts,
)
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.models import CommandReceipt
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_evidence import evidence as evidence
from tests.integration.business.test_refund_revision_headers import replacement
from tests.integration.business.test_refund_service import refund
from tests.integration.conftest import refund_fixed_receipt_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def response(c, identifier):
    return c.scalar(
        text("SELECT coinpup_business_refund_revision_response(:id)"), {"id": identifier}
    )


def prove(c, identifier):
    c.execute(text("SELECT coinpup_validate_refund_revision_receipt(:id)"), {"id": identifier})


def add_receipt(c, entry, body, **changes):
    c.execute(
        insert(CommandReceipt).values(
            **(
                dict(
                    ledger_id=entry["ledger_id"],
                    key=entry["idempotency_key"],
                    request_hash="a" * 64,
                    hash_version=2,
                    operation_id=entry["operation_id"],
                    response_status=200,
                    response=body,
                )
                | changes
            )
        )
    )


def correct(c, setup, *, keep_evidence, **changes):
    s, header, body, _, link = setup
    row = (
        header
        | replacement(body)
        | dict(
            idempotency_key="fictional-first-correction",
            asset_id="EUR",
            amount=Decimal("18.73"),
            payment_amount=Decimal("19.00"),
            fees=[
                dict(
                    account_id=str(body.account_id),
                    category_id=str(s["expenses"][0].id),
                    asset_id="BTC",
                    amount=amount,
                )
                for amount in ("0.00000002", "0.00000001")
            ],
        )
        | changes
    )
    c.execute(insert(Revision).values(**row))
    if keep_evidence:
        c.execute(insert(Evidence).values(**link))
    return row


def terminal(c, setup, prior, **changes):
    _, header, _, _, _ = setup
    row = (
        header
        | dict(
            id=uuid4(),
            operation_version=prior["operation_version"] + 1,
            previous_revision_id=prior["id"],
            previous_journal_id=prior["replacement_journal_id"],
            reversal_journal_id=uuid4(),
            idempotency_key="fictional-cancellation",
        )
        | changes
    )
    c.execute(insert(Revision).values(**row))
    return row


def test_fixed_receipts_keep_original_and_every_intermediate_after_changed_evidence_and_cancel(
    evidence,
):
    s, _, command, original, link = evidence
    before = snapshot(s)
    with s["engine"].connect() as c:
        first = correct(c, evidence, keep_evidence=False)
        one = response(c, first["id"])
        assert validate_refund_revision_amounts(RefundRevisionResponse.model_validate(one), ASSETS)
        assert (one["previous_version"], one["version"]) == (1, 2)
        assert one["action"] == "correct" and one["revision_id"] == str(first["id"])
        assert one["previous"] == dict(
            account_id=str(command.account_id),
            asset_id=original.asset_id,
            payment_asset_id=original.payment_asset_id,
            amount=original.amount,
            payment_amount=original.payment_amount,
            source_version=1,
            reason=command.reason,
            transaction_date=command.transaction_date.isoformat(),
            recognition_date=command.transaction_date.isoformat(),
            fees=[],
            allocation_return_ids=[str(link["return_id"])],
        )
        assert one["replacement"] == dict(
            account_id=str(first["account_id"]),
            asset_id="EUR",
            payment_asset_id="USD",
            amount="18.730000000000000000",
            payment_amount="19.000000000000000000",
            source_version=1,
            reason=first["reason"],
            transaction_date=first["transaction_date"].isoformat(),
            recognition_date=first["transaction_date"].isoformat(),
            fees=first["fees"],
            allocation_return_ids=[],
        )
        add_receipt(c, first, one)
        prove(c, first["id"])
        second = correct(
            c,
            evidence,
            keep_evidence=False,
            id=uuid4(),
            operation_version=3,
            previous_revision_id=first["id"],
            previous_journal_id=first["replacement_journal_id"],
            idempotency_key="fictional-second-correction",
            reversal_journal_id=uuid4(),
            replacement_journal_id=uuid4(),
            fees=[],
            asset_id="USD",
            amount=Decimal("7.01"),
            payment_amount=Decimal("7.01"),
        )
        c.execute(insert(Evidence).values(**(link | dict(id=uuid4(), revision_id=second["id"]))))
        two = response(c, second["id"])
        assert two["previous"] == one["replacement"]
        assert (
            two["replacement"]["allocation_return_ids"] == one["previous"]["allocation_return_ids"]
        )
        assert two["replacement"]["fees"] == []
        assert two["previous_journal_id"] == one["replacement_journal_id"]
        add_receipt(c, second, two)
        last = terminal(c, evidence, second)
        three = response(c, last["id"])
        assert three["previous"] == two["replacement"] and three["previous_version"] == 3
        assert three["version"] == 4 and three["action"] == "cancel"
        assert three["replacement"] is None and three["replacement_journal_id"] is None
        add_receipt(c, last, three)
        for row, expected in ((first, one), (second, two), (last, three)):
            assert validate_refund_revision_amounts(
                RefundRevisionResponse.model_validate(expected), ASSETS
            )
            assert response(c, row["id"]) == expected
            prove(c, row["id"])
        with pytest.raises(IntegrityError) as closed:
            c.exec_driver_sql("SET CONSTRAINTS tr_business_refund_revisions_complete IMMEDIATE")
        assert closed.value.orig.diag.constraint_name == "ck_refund_revision_shape"
    assert refund(s, command) == original
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "bad",
    [
        "missing",
        "status",
        "hash",
        "anchor",
        "extra",
        "fees",
        "source",
        "version",
        "amount",
        "reversal",
    ],
)
def test_fixed_receipt_rejects_every_wrong_permanent_anchor_or_payload(evidence, bad):
    s, _, _, original, _ = evidence
    before = snapshot(s)
    with s["engine"].connect() as c:
        row = correct(c, evidence, keep_evidence=True)
        expected = response(c, row["id"])
        changed = deepcopy(expected)
        attrs = {}
        if bad == "status":
            attrs["response_status"] = 201
        if bad == "hash":
            attrs["hash_version"] = 1
        if bad == "anchor":
            attrs["operation_id"] = c.scalar(
                text("SELECT operation_id FROM business_payments WHERE id=:id"),
                {"id": original.payment_id},
            )
        if bad == "extra":
            changed["extra"] = None
        if bad == "fees":
            changed["replacement"]["fees"].reverse()
        if bad == "source":
            changed["previous"]["source_version"] = 1.0
        if bad == "version":
            changed["version"] = 2.0
        if bad == "amount":
            changed["replacement"]["payment_amount"] = "19"
        if bad == "reversal":
            changed["reversal_journal_id"] = expected["previous_journal_id"]
        if bad != "missing":
            add_receipt(c, row, changed, **attrs)
        with pytest.raises(IntegrityError) as failure:
            prove(c, row["id"])
        assert failure.value.orig.diag.constraint_name == "ck_refund_revision_shape"
    assert snapshot(s) == before


@pytest.mark.parametrize("bad", ["missing", "stale", "different", "post_cancel"])
def test_response_requires_the_immediately_previous_normal_posting(evidence, bad):
    s, header, _, original, _ = evidence
    before = snapshot(s)
    with s["engine"].connect() as c:
        first = correct(c, evidence, keep_evidence=False)
        if bad == "missing":
            identifier = uuid4()
        elif bad == "post_cancel":
            cancelled = terminal(c, evidence, first)
            next_row = header | dict(
                id=uuid4(),
                operation_version=4,
                previous_revision_id=cancelled["id"],
                previous_journal_id=first["replacement_journal_id"],
                reversal_journal_id=uuid4(),
                idempotency_key="fictional-illegal-after-terminal",
            )
            c.execute(insert(Revision).values(**next_row))
            identifier = next_row["id"]
        else:
            prior = original.journal_id if bad == "stale" else uuid4()
            identifier = terminal(c, evidence, first, previous_journal_id=prior)["id"]
        with pytest.raises(IntegrityError) as failure:
            response(c, identifier)
        assert failure.value.orig.diag.constraint_name == "ck_refund_revision_shape"
    assert snapshot(s) == before


def test_migration_round_trip_preserves_legacy_shape_and_closed_gate(evidence):
    s, _, command, original, _ = evidence
    before = snapshot(s)
    lookup = """SELECT proname,pg_get_functiondef(oid) FROM pg_proc
        WHERE pronamespace='public'::regnamespace
        AND proname IN ('coinpup_business_refund_revision_response',
        'coinpup_validate_refund_revision_receipt',
        'coinpup_business_refund_response','coinpup_check_refund_revision') ORDER BY proname"""
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        defs = dict(c.exec_driver_sql(lookup).all())
        assert len(defs) == 4
        migration = refund_fixed_receipt_migration()
        migration["downgrade"]()
        assert dict(c.exec_driver_sql(lookup).all()) == {
            k: defs[k]
            for k in ("coinpup_business_refund_response", "coinpup_check_refund_revision")
        }
        migration["upgrade"]()
        assert dict(c.exec_driver_sql(lookup).all()) == defs
    assert refund(s, command) == original and snapshot(s) == before


def test_downgrade_refuses_protected_revision(evidence):
    s = evidence[0]
    before = snapshot(s)
    with s["engine"].connect() as c, Operations.context(MigrationContext.configure(c)):
        correct(c, evidence, keep_evidence=True)
        with pytest.raises(IntegrityError) as rejected:
            refund_fixed_receipt_migration()["downgrade"]()
        assert rejected.value.orig.diag.constraint_name == "ck_refund_revision_downgrade"
    assert snapshot(s) == before
