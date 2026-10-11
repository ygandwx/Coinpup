"""Fictional confirmation storage remains fail-closed before accrual activation."""

from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.confirmation_models import BusinessConfirmation, BusinessPosting
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_draft_lines import document, line, save
from tests.integration.conftest import business_confirmation_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def seed_source(s):
    parent = document(s)
    source = line(s, parent)
    save(s, source)
    return dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        document_id=parent,
        created_by=s["owner"],
        source_version=1,
        idempotency_key="fictional-confirmation",
    ), source


def test_source_capture_keeps_exact_strings_and_notifications_rollback(ledger_setup):
    s = ledger_setup
    values, source = seed_source(s)
    before = snapshot(s)
    with s["engine"].connect() as c:
        transaction = c.begin()
        try:
            c.execute(insert(BusinessConfirmation).values(**values))
            proof = c.scalar(select(BusinessConfirmation.snapshot))
            assert proof["document"]["id"] == str(values["document_id"])
            assert proof["lines"][0]["quantity"] == source["quantity"]
            assert proof["lines"][0]["total_amount"] == "54.130000000000000000"
            assert isinstance(proof["lines"][0]["net_amount"], str)
            assert c.scalar(
                select(ChangeLog.entity_id).where(ChangeLog.entity_type == "business_confirmations")
            ) == str(values["id"])
        finally:
            transaction.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "field,value,constraint",
    [
        ("source_version", 2, "ck_business_confirmation_source"),
        ("version", 2, "ck_business_confirmation_version"),
        ("snapshot", {}, "ck_business_confirmation_source"),
        ("document_id", "other", "ck_business_confirmation_source"),
        ("ledger_id", "other", "ck_business_confirmation_source"),
        ("created_by", "other", "fk_business_confirmation_owner"),
    ],
)
def test_wrong_source_or_identity_is_rejected_atomically(ledger_setup, field, value, constraint):
    s = ledger_setup
    values, _ = seed_source(s)
    before = snapshot(s)
    values[field] = uuid4() if value == "other" else value
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        c.execute(insert(BusinessConfirmation).values(**values))
    assert error.value.orig.diag.constraint_name == constraint
    assert snapshot(s) == before


def test_unbacked_confirmation_cannot_commit(ledger_setup):
    s = ledger_setup
    values, _ = seed_source(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError), s["engine"].begin() as c:
        c.execute(insert(BusinessConfirmation).values(**values))
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "table,action",
    [
        (BusinessConfirmation, "update"),
        (BusinessConfirmation, "delete"),
        (BusinessPosting, "update"),
        (BusinessPosting, "delete"),
    ],
)
def test_pending_membership_is_already_immutable(ledger_setup, table, action):
    s = ledger_setup
    values, source = seed_source(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        c.execute(insert(BusinessConfirmation).values(**values))
        if table is BusinessPosting:
            c.execute(
                insert(table).values(
                    id=uuid4(),
                    confirmation_id=values["id"],
                    document_id=values["document_id"],
                    ledger_id=s["ledger"],
                    line_id=source["id"],
                    operation_id=uuid4(),
                    journal_id=uuid4(),
                )
            )
        statement = delete(table) if action == "delete" else update(table).values(version=2)
        c.execute(statement)
    expected = (
        "ck_business_confirmation_immutable"
        if table is BusinessConfirmation
        else "ck_business_posting_immutable"
    )
    assert error.value.orig.diag.constraint_name == expected
    assert snapshot(s) == before


def test_empty_round_trip_and_pending_history_downgrade_guard(ledger_setup):
    s = ledger_setup
    migration = business_confirmation_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        migration["downgrade"]()
        migration["upgrade"]()
    values, _ = seed_source(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        c.execute(insert(BusinessConfirmation).values(**values))
        with Operations.context(MigrationContext.configure(c)):
            migration["downgrade"]()
    assert error.value.orig.diag.constraint_name == "ck_business_confirmation_downgrade"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "case,constraint",
    [
        ("foreign_line", "fk_business_posting_line"),
        ("foreign_ledger", "fk_business_posting_confirmation"),
        ("line_id", "uq_business_posting_line"),
        ("operation_id", "uq_business_posting_operation"),
        ("journal_id", "uq_business_posting_journal"),
    ],
)
def test_membership_scope_and_unique_identities(ledger_setup, case, constraint):
    s = ledger_setup
    values, source = seed_source(s)
    second_source = line(s, values["document_id"], line_no=2)
    save(s, second_source)
    foreign = document(s)
    foreign_line = line(s, foreign)
    save(s, foreign_line)
    first = dict(
        id=uuid4(),
        confirmation_id=values["id"],
        ledger_id=s["ledger"],
        document_id=values["document_id"],
        line_id=source["id"],
        operation_id=uuid4(),
        journal_id=uuid4(),
    )
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        c.execute(insert(BusinessConfirmation).values(**values))
        if case == "foreign_line":
            c.execute(insert(BusinessPosting).values(**(first | {"line_id": foreign_line["id"]})))
        elif case == "foreign_ledger":
            c.execute(insert(BusinessPosting).values(**(first | {"ledger_id": uuid4()})))
        else:
            c.execute(insert(BusinessPosting).values(**first))
            second = first | {
                "id": uuid4(),
                "line_id": second_source["id"],
                "operation_id": uuid4(),
                "journal_id": uuid4(),
            }
            second[case] = first[case]
            c.execute(insert(BusinessPosting).values(**second))
    expected = {constraint}
    if case == "foreign_ledger":
        expected.add("fk_business_posting_line")
    assert error.value.orig.diag.constraint_name in expected
    assert snapshot(s) == before
