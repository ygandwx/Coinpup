"""Closed historical leafs validate real postings without trusting today's refund pointer."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.refund_models import BusinessRefund
from coinpup_api.business.refund_revision_models import BusinessRefundRevision as Revision
from coinpup_api.business.refund_revision_return_models import (
    BusinessRefundRevisionReturn as Evidence,
)
from coinpup_api.business.refund_schemas import BusinessRefundCreate
from coinpup_api.ledger.models import CommandReceipt, FinancialOperation
from coinpup_api.sync.locking import acquire_write_lock
from sqlalchemy import insert, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_credit_fact_validation import copy_posting, stored
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_evidence import evidence as evidence
from tests.integration.business.test_refund_revision_headers import replacement
from tests.integration.business.test_refund_service import refund, setup_refund
from tests.integration.conftest import refund_historical_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ledger.test_posting_service_database import opening

pytestmark = pytest.mark.integration


def check_origin(c, identifier):
    c.execute(text("SELECT coinpup_validate_refund_origin_facts(:id)"), {"id": identifier})


def check_posting(c, journal, *, forged_common=False):
    common = "'{}'::jsonb" if forged_common else "coinpup_validate_journal_common(:id)"
    c.execute(text(f"SELECT coinpup_validate_refund_posting_fact(:id,{common})"), {"id": journal})


@pytest.mark.parametrize("direction", ["receipt", "payout"])
@pytest.mark.parametrize("asset,amount", [("USD", "20"), ("EUR", "18.97")])
def test_committed_origin_proves_each_principal_and_both_ordered_third_asset_fees(
    drafts, direction, asset, amount
):
    s = drafts
    _, _, command = setup_refund(s, direction, asset, amount)
    s["posting"].post_opening(
        s["owner"], s["ledger"], opening(s, "1", "BTC"), "fictional-refund-fees"
    )
    command = BusinessRefundCreate.model_validate(
        command.model_dump()
        | dict(
            fees=[
                dict(
                    account_id=command.account_id,
                    category_id=s["expenses"][0].id,
                    asset_id="BTC",
                    amount=fee_amount,
                )
                for fee_amount in ("0.00000001", "0.00000002")
            ]
        )
    )
    original = refund(s, command)
    before = snapshot(s)
    with s["engine"].connect() as c:
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        check_origin(c, original.id)
        check_posting(c, original.journal_id)
    assert refund(s, command) == original
    assert snapshot(s) == before


def pending(c, setup, *, facts=None, posting=None, link_changes=None, line_changes=None):
    s, header, command, original, link = setup
    corrected = (
        header
        | replacement(command)
        | dict(amount=Decimal(original.amount), payment_amount=Decimal(original.payment_amount))
        | (facts or {})
    )
    acquire_write_lock(c)
    c.execute(insert(Revision).values(**corrected))
    c.execute(insert(Evidence).values(**(link | (link_changes or {}))))
    journal = corrected["replacement_journal_id"]
    c.execute(
        update(FinancialOperation)
        .where(FinancialOperation.id == original.operation_id)
        .values(version=2, current_journal_id=journal)
    )
    copy_posting(
        c,
        original.journal_id,
        dict(
            id=journal,
            operation_version=2,
            transaction_date=corrected["transaction_date"],
            recognition_date=corrected["transaction_date"],
            description=corrected["reason"],
            revision_reason=corrected["revision_reason"],
        )
        | (posting or {}),
        line_changes=line_changes,
    )
    return corrected


@pytest.mark.parametrize("problem", ["order", "amount", "asset", "extra-field"])
def test_balanced_original_fee_legs_cannot_prove_different_replacement_fees(drafts, problem):
    s = drafts
    _, _, body = setup_refund(s, allocated=False)
    fees = [
        dict(
            account_id=str(body.account_id),
            category_id=str(s["expenses"][0].id),
            asset_id="USD",
            amount=amount,
        )
        for amount in ("0.02", "0.01")
    ]
    body = BusinessRefundCreate.model_validate(body.model_dump() | {"fees": fees})
    original = refund(s, body)
    before = snapshot(s)
    version = (
        dict(
            id=uuid4(),
            ledger_id=s["ledger"],
            created_by=s["owner"],
            refund_id=original.id,
            payment_id=original.payment_id,
            operation_id=original.operation_id,
            operation_version=2,
            previous_journal_id=original.journal_id,
            reversal_journal_id=uuid4(),
            revision_reason="Fictional fee replacement",
            idempotency_key="fictional-fee-revision",
        )
        | replacement(body)
        | dict(amount=Decimal(body.amount), payment_amount=Decimal(body.payment_amount))
    )
    changed = {
        "order": fees[::-1],
        "amount": [fees[0] | {"amount": "0.020"}, fees[1]],
        "asset": [fees[0], fees[1] | {"asset_id": "BTC"}],
        "extra-field": [fees[0] | {"extra": "fictional"}, fees[1]],
    }[problem]
    with s["engine"].connect() as c:
        acquire_write_lock(c)
        c.execute(insert(Revision).values(**(version | {"fees": changed})))
        copy_posting(
            c,
            original.journal_id,
            dict(
                id=version["replacement_journal_id"],
                operation_version=2,
                transaction_date=version["transaction_date"],
                recognition_date=version["transaction_date"],
                description=version["reason"],
                revision_reason=version["revision_reason"],
            ),
        )
        with pytest.raises(IntegrityError) as rejected:
            check_posting(c, version["replacement_journal_id"])
        assert rejected.value.orig.diag.constraint_name == "ck_business_refund_shape"
    assert refund(s, body) == original
    assert snapshot(s) == before


def test_original_and_replacement_validate_even_after_the_child_head_moves(evidence):
    s, header, command, original, _ = evidence
    before = snapshot(s)
    with s["engine"].connect() as c:
        corrected = pending(c, evidence)
        journal = corrected["replacement_journal_id"]
        check_origin(c, original.id)
        check_posting(c, journal)
        c.execute(
            insert(Revision).values(
                **(
                    header
                    | dict(
                        id=uuid4(),
                        operation_version=3,
                        previous_revision_id=corrected["id"],
                        previous_journal_id=journal,
                        reversal_journal_id=uuid4(),
                        idempotency_key="fictional-later-cancellation",
                    )
                )
            )
        )
        c.execute(
            update(FinancialOperation)
            .where(FinancialOperation.id == original.operation_id)
            .values(version=3, status="cancelled")
        )
        check_origin(c, original.id)
        check_posting(c, journal)
        with pytest.raises(IntegrityError) as closed:
            c.exec_driver_sql("SET CONSTRAINTS tr_business_refund_revisions_complete IMMEDIATE")
        assert closed.value.orig.diag.constraint_name == "ck_refund_revision_shape"
        c.rollback()
    assert refund(s, command) == original
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "change",
    [
        {"facts": {"amount": Decimal("19")}},
        {"facts": {"payment_amount": Decimal("19.001")}},
        {"facts": {"asset_id": "EUR"}},
        {"facts": {"transaction_date": date(2026, 9, 30)}},
        {"posting": {"revision_reason": "Fictional wrong revision"}},
        {"posting": {"description": "Fictional wrong description"}},
        {"posting": {"recognition_date": date(2026, 10, 4)}},
        {"link_changes": {"line_no": 2}},
        {"line_changes": {"party_id": None}},
    ],
)
def test_leaf_rejects_wrong_amount_asset_date_dimensions_or_evidence_before_commit(
    evidence, change
):
    s = evidence[0]
    before = snapshot(s)
    with s["engine"].connect() as c:
        corrected = pending(c, evidence, **change)
        with pytest.raises(IntegrityError) as rejected:
            check_posting(c, corrected["replacement_journal_id"])
        assert rejected.value.orig.diag.constraint_name == "ck_business_refund_shape"
    assert snapshot(s) == before


@pytest.mark.parametrize("target", ["unknown", "forged", "source_operation"])
def test_read_only_leaf_requires_real_source_original_and_common_result(evidence, target):
    s, _, _, original, _ = evidence
    before = snapshot(s)
    with s["engine"].connect() as c:
        if target == "source_operation":
            operation = c.scalar(
                text("SELECT operation_id FROM business_payments WHERE id=:id"),
                {"id": original.payment_id},
            )
            c.execute(
                update(FinancialOperation)
                .where(FinancialOperation.id == operation)
                .values(status="cancelled", version=2)
            )
        with pytest.raises(IntegrityError) as rejected:
            check_posting(
                c,
                uuid4() if target == "unknown" else original.journal_id,
                forged_common=target == "forged",
            )
        assert rejected.value.orig.diag.constraint_name == "ck_business_refund_shape"
    assert snapshot(s) == before


@pytest.mark.parametrize("problem", ["missing", "extra", "status", "anchor", "hash"])
def test_origin_requires_the_exact_legacy_201_even_for_rollback_only_rows(evidence, problem):
    s, _, _, original, _ = evidence
    before = snapshot(s)
    with s["engine"].connect() as c:
        acquire_write_lock(c)
        identifier, operation, journal, key = uuid4(), uuid4(), uuid4(), str(uuid4())
        clone = stored(c, BusinessRefund, original.id) | dict(
            id=identifier, operation_id=operation, journal_id=journal, idempotency_key=key
        )
        c.execute(insert(BusinessRefund).values(**clone))
        c.execute(
            insert(FinancialOperation).values(
                **(
                    stored(c, FinancialOperation, original.operation_id)
                    | dict(id=operation, current_journal_id=journal)
                )
            )
        )
        copy_posting(c, original.journal_id, dict(id=journal, operation_id=operation))
        if problem != "missing":
            response = c.scalar(
                text("SELECT coinpup_business_refund_response(:id)"), {"id": identifier}
            )
            c.execute(
                insert(CommandReceipt).values(
                    ledger_id=s["ledger"],
                    key=key,
                    request_hash="b" * 64,
                    hash_version=1 if problem == "hash" else 2,
                    response_status=200 if problem == "status" else 201,
                    response=response
                    | ({"reason": "Fictional extra"} if problem == "extra" else {}),
                    operation_id=original.operation_id if problem == "anchor" else operation,
                )
            )
        check_posting(c, journal)
        with pytest.raises(IntegrityError) as rejected:
            check_origin(c, identifier)
        assert rejected.value.orig.diag.constraint_name == "ck_business_refund_shape"
    assert snapshot(s) == before


def test_migration_round_trip_preserves_previous_dispatch_and_real_receipt(evidence):
    s, _, command, original, _ = evidence
    before = snapshot(s)
    lookup = """SELECT proname,pg_get_functiondef(oid) FROM pg_proc
        WHERE pronamespace='public'::regnamespace AND proname IN
        ('coinpup_validate_refund_origin_facts','coinpup_validate_refund_posting_fact',
         'coinpup_validate_business_refund','coinpup_validate_shape_refund',
         'coinpup_refund_posting_fact','coinpup_business_refund_response') ORDER BY proname"""
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        functions = dict(c.exec_driver_sql(lookup).all())
        assert len(functions) == 6
        migration = refund_historical_migration()
        migration["downgrade"]()
        assert dict(c.exec_driver_sql(lookup).all()) == {
            name: definition
            for name, definition in functions.items()
            if name
            not in {"coinpup_validate_refund_origin_facts", "coinpup_validate_refund_posting_fact"}
        }
        migration["upgrade"]()
        assert dict(c.exec_driver_sql(lookup).all()) == functions
        check_origin(c, original.id)
    assert refund(s, command) == original
    assert snapshot(s) == before


def test_downgrade_requires_no_refund_revision_history(evidence):
    s, header, _, _, _ = evidence
    before = snapshot(s)
    with s["engine"].begin() as c:
        c.execute(insert(Revision).values(**header))
        with (
            Operations.context(MigrationContext.configure(c)),
            pytest.raises(IntegrityError) as caught,
        ):
            refund_historical_migration()["downgrade"]()
        assert caught.value.orig.diag.constraint_name == "ck_refund_revision_downgrade"
        c.rollback()
    assert snapshot(s) == before
