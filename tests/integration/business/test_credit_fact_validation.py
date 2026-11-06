"""Actual originals and rollback-only historical facts; full revisions remain gated."""

from datetime import date
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.credit_models import BusinessCredit, BusinessCreditLine
from coinpup_api.business.credit_revision_member_models import (
    BusinessCreditRevisionMember as Member,
)
from coinpup_api.business.credit_revision_models import BusinessCreditRevision as Revision
from coinpup_api.ledger.models import CommandReceipt, FinancialOperation, Journal, JournalLine
from coinpup_api.sync.locking import acquire_write_lock
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_credit_revision_member_storage import member
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_credit_revision_storage import revision as revision
from tests.integration.business.test_credit_service import credit, setup_credit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import credit_fact_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def stored(connection, model, identifier):
    return dict(
        connection.execute(select(model.__table__).where(model.id == identifier)).mappings().one()
    )


def leaf(connection, identifier, supplied=None):
    common = (
        "coinpup_validate_journal_common(:id)" if supplied is None else "CAST(:supplied AS jsonb)"
    )
    connection.execute(
        text(f"SELECT coinpup_validate_credit_posting_fact(:id,{common})"),
        {"id": identifier, "supplied": supplied},
    )


def origin(connection, identifier):
    connection.execute(text("SELECT coinpup_validate_credit_origin_facts(:id)"), {"id": identifier})


def lock_credit(connection, original):
    acquire_write_lock(connection)
    entity = connection.scalar(
        text("SELECT entity_id FROM ledgers WHERE id=:id FOR UPDATE"), {"id": original.ledger_id}
    )
    connection.execute(text("SELECT id FROM entities WHERE id=:id FOR UPDATE"), {"id": entity})
    connection.execute(
        text("SELECT asset_id FROM assets WHERE asset_id=:id ORDER BY asset_id FOR UPDATE"),
        {"id": original.asset_id},
    )


def copy_posting(connection, source_journal, values, *, reverse=False, line_changes=None):
    header = stored(connection, Journal, source_journal) | dict(sealed=False) | values
    connection.execute(insert(Journal).values(**header))
    lines = (
        connection.execute(
            select(JournalLine.__table__).where(JournalLine.journal_id == source_journal)
        )
        .mappings()
        .all()
    )
    for line in lines:
        copied = dict(line) | dict(id=uuid4(), journal_id=header["id"])
        if reverse:
            copied["amount"] = copied["amount"].copy_negate()
        connection.execute(insert(JournalLine).values(**(copied | (line_changes or {}))))


def pending_fact(
    connection,
    header,
    original,
    *,
    action="correct",
    changes=None,
    journal_changes=None,
    line_changes=None,
):
    lock_credit(connection, original)
    first = original.postings[0]
    row = member(header, original, action)
    if action != "cancel":
        row.update(
            net_amount=first.net_amount,
            tax_amount=first.tax_amount,
            total_amount=first.total_amount,
            journal_id=uuid4(),
        )
    if action == "create":
        row["operation_id"] = uuid4()
        born = stored(connection, FinancialOperation, first.operation_id) | dict(
            id=row["operation_id"], current_journal_id=row["journal_id"]
        )
        connection.execute(insert(FinancialOperation).values(**born))
    else:
        connection.execute(
            update(FinancialOperation)
            .where(FinancialOperation.id == first.operation_id)
            .values(
                version=2,
                status="cancelled" if action == "cancel" else "active",
                current_journal_id=first.journal_id if action == "cancel" else row["journal_id"],
            )
        )
        copy_posting(
            connection,
            first.journal_id,
            dict(
                id=row["reversal_journal_id"],
                operation_version=2,
                journal_kind="reversal",
                reverses_journal_id=first.journal_id,
                revision_reason=header["revision_reason"],
            ),
            reverse=True,
        )
    connection.execute(insert(Revision).values(**header))
    if action != "cancel":
        copy_posting(
            connection,
            first.journal_id,
            dict(
                id=row["journal_id"],
                operation_id=row["operation_id"],
                operation_version=row["operation_version"],
                transaction_date=header["credit_date"],
                recognition_date=row["recognition_date"],
                description=header["credit_reason"],
                revision_reason=header["revision_reason"] if action == "correct" else None,
            )
            | (journal_changes or {}),
            line_changes=line_changes,
        )
    connection.execute(insert(Member).values(**(row | (changes or {}))))
    return row


def pending_origin(
    connection,
    original,
    *,
    header_changes=None,
    line_changes=None,
    receipt_changes=None,
    omit_receipt=False,
):
    lock_credit(connection, original)
    first = original.postings[0]
    identifier, operation, journal = uuid4(), uuid4(), uuid4()
    header = (
        stored(connection, BusinessCredit, original.id)
        | dict(id=identifier, idempotency_key=str(identifier))
        | (header_changes or {})
    )
    connection.execute(insert(BusinessCredit).values(**header))
    connection.execute(
        insert(FinancialOperation).values(
            **(
                stored(connection, FinancialOperation, first.operation_id)
                | dict(id=operation, current_journal_id=journal)
            )
        )
    )
    copy_posting(connection, first.journal_id, dict(id=journal, operation_id=operation))
    row = dict(
        connection.execute(
            select(BusinessCreditLine.__table__).where(BusinessCreditLine.credit_id == original.id)
        )
        .mappings()
        .one()
    )
    connection.execute(
        insert(BusinessCreditLine).values(
            **(
                row
                | dict(id=uuid4(), credit_id=identifier, operation_id=operation, journal_id=journal)
                | (line_changes or {})
            )
        )
    )
    if not omit_receipt:
        response = connection.scalar(
            text("SELECT coinpup_business_credit_response(:id)"), {"id": identifier}
        )
        connection.execute(
            insert(CommandReceipt).values(
                **(
                    dict(
                        ledger_id=header["ledger_id"],
                        key=header["idempotency_key"],
                        request_hash="a" * 64,
                        hash_version=2,
                        response_status=201,
                        response=response,
                        operation_id=operation,
                    )
                    | (receipt_changes or {})
                )
            )
        )
    return identifier


@pytest.mark.parametrize("kind", ["invoice", "bill"])
@pytest.mark.parametrize("count", [1, 200])
def test_original_facts_and_journals_validate_read_only_without_changing_receipts(
    drafts, kind, count
):
    s = drafts
    _, _, _, request = setup_credit(s, kind, count=count)
    original = credit(s, request)
    before = snapshot(s)
    with s["engine"].connect() as connection:
        origin(connection, original.id)
        leaf(connection, original.postings[-1].journal_id)
    assert credit(s, request) == original
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "action,changes",
    [
        ("correct", None),
        ("create", None),
        ("cancel", None),
        ("correct", {"net_amount": "18", "tax_amount": "2"}),
    ],
)
def test_old_and_replacement_facts_use_exact_journals_before_the_closed_gate(
    revision, action, changes
):
    s, header, _, original = revision
    if action == "cancel":
        header = header | dict(action="cancel", credit_date=None, credit_reason=None)
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        row = pending_fact(connection, header, original, action=action, changes=changes)
        leaf(connection, original.postings[0].journal_id)
        if row["journal_id"] is not None:
            leaf(connection, row["journal_id"])
        origin(connection, original.id)
        # No valid leaf result is permission to commit an incomplete group or reused receipt.
        with pytest.raises(IntegrityError) as failure:
            connection.exec_driver_sql(
                "SET CONSTRAINTS tr_business_credit_revisions_complete IMMEDIATE"
            )
        assert failure.value.orig.diag.constraint_name == "ck_credit_revision_shape"
        transaction.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "member_changes,journal_changes,line_changes",
    [
        ({"net_amount": "1", "tax_amount": "19"}, None, None),
        ({"net_amount": "1", "tax_amount": "0", "total_amount": "1"}, None, None),
        ({"recognition_date": date(2026, 11, 5)}, None, None),
        (None, {"transaction_date": date(2026, 11, 5)}, None),
        (None, {"description": "Fictional wrong replacement"}, None),
        (None, {"revision_reason": "Fictional wrong reason"}, None),
        (None, None, {"project_id": None}),
        (None, None, {"document_line_id": None}),
        (None, None, {"party_id": None}),
        (None, None, {"amount": "1"}),
    ],
)
def test_historical_fact_mismatches_fail_the_leaf_not_the_commit_gate(
    revision, member_changes, journal_changes, line_changes
):
    s, header, _, original = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        row = pending_fact(
            connection,
            header,
            original,
            changes=member_changes,
            journal_changes=journal_changes,
            line_changes=line_changes,
        )
        leaf(connection, row["journal_id"])
        pytest.fail("The requested leaf did not reject a mismatching fact")
    assert failure.value.orig.diag.constraint_name == "ck_business_credit_shape"
    assert snapshot(s) == before


@pytest.mark.parametrize("supplied", ["{}", "null", '{"error_message":"forged"}'])
def test_arbitrary_json_cannot_replace_the_actual_common_check(revision, supplied):
    s, _, _, original = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        leaf(connection, original.postings[0].journal_id, supplied)
    assert failure.value.orig.diag.constraint_name == "ck_business_credit_shape"
    assert snapshot(s) == before


@pytest.mark.parametrize("target", ["missing", "source_posting", "duplicate"])
def test_journal_identity_requires_exactly_one_credit_fact(revision, target):
    s, header, _, original = revision
    first = original.postings[0]
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        identifier = uuid4()
        if target == "source_posting":
            identifier = connection.scalar(
                select(FinancialOperation.current_journal_id).where(
                    FinancialOperation.id == first.source_operation_id
                )
            )
        if target == "duplicate":
            connection.execute(insert(Revision).values(**header))
            connection.execute(insert(Member).values(**member(header, original, "create")))
            identifier = first.journal_id
        leaf(connection, identifier)
        pytest.fail("A missing or duplicate canonical fact was accepted")
    assert failure.value.orig.diag.constraint_name == "ck_business_credit_shape"
    assert snapshot(s) == before


def test_a_new_original_creation_proof_is_valid_before_rollback(revision):
    s, _, _, original = revision
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        identifier = pending_origin(connection, original)
        origin(connection, identifier)
        transaction.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"receipt_changes": {"hash_version": 1}},
        {"receipt_changes": {"response_status": 200}},
        {"receipt_changes": {"response": {}}},
        {"omit_receipt": True},
        {"header_changes": {"source_version": 99}},
        {"line_changes": {"line_no": 2}},
    ],
)
def test_origin_still_requires_the_exact_original_creation_proof(revision, changes):
    s, _, _, original = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        identifier = pending_origin(connection, original, **changes)
        origin(connection, identifier)
        pytest.fail("Original creation proof must fail before any deferred check")
    assert failure.value.orig.diag.constraint_name == "ck_business_credit_shape"
    assert snapshot(s) == before


def test_empty_fact_migration_round_trip_preserves_all_original_functions_and_receipts(revision):
    s, _, request, original = revision
    before = snapshot(s)
    with (
        s["engine"].begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        functions, triggers = definitions(connection)
        migration = credit_fact_migration()
        migration["downgrade"]()
        assert definitions(connection) == (
            [
                body
                for body in functions
                if not any(
                    name in body.split("\n", 1)[0]
                    for name in (
                        "coinpup_validate_credit_origin_facts",
                        "coinpup_validate_credit_posting_fact",
                    )
                )
            ],
            triggers,
        )
        migration["upgrade"]()
        assert definitions(connection) == (functions, triggers)
    assert credit(s, request) == original
    assert snapshot(s) == before


@pytest.mark.parametrize("notification_only", [False, True])
def test_fact_helper_downgrade_refuses_protected_revision_history(revision, notification_only):
    s, header, _, _ = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        if notification_only:
            connection.exec_driver_sql(
                "CREATE TEMP TABLE business_credit_revisions "
                "(id uuid, ledger_id uuid, version int) ON COMMIT DROP; "
                "CREATE TRIGGER fictional_fact_history AFTER INSERT "
                "ON pg_temp.business_credit_revisions "
                "FOR EACH ROW EXECUTE FUNCTION public.coinpup_record_change()"
            )
            connection.execute(
                text("INSERT INTO pg_temp.business_credit_revisions VALUES (:id,:ledger,1)"),
                dict(id=uuid4(), ledger=s["ledger"]),
            )
        else:
            connection.execute(insert(Revision).values(**header))
        with Operations.context(MigrationContext.configure(connection)):
            credit_fact_migration()["downgrade"]()
    assert failure.value.orig.diag.constraint_name == "ck_credit_revision_downgrade"
    assert snapshot(s) == before
