"""Fictional incomplete credit groups roll back and cannot rewrite source history."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.credit_models import BusinessCredit, BusinessCreditLine
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import DataError, IntegrityError

from tests.integration.business.test_confirmation_service import prepare, submit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import credit_storage_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def credit_rows(s):
    document, payload = prepare(s)
    confirmed = submit(s, document, payload)
    posting = confirmed.postings[0]
    header = dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        document_id=document.id,
        source_version=confirmed.document_version,
        credit_date=date(2026, 10, 21),
        reason="Fictional gated credit 虚构贷项",
        created_by=s["owner"],
        idempotency_key="fictional-gated-credit",
    )
    member = dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        credit_id=header["id"],
        document_id=document.id,
        source_line_id=posting.line_id,
        source_operation_id=posting.operation_id,
        line_no=1,
        net_amount=Decimal("17.23"),
        tax_amount=Decimal("2.77"),
        total_amount=Decimal("20"),
        recognition_date=date(2026, 10, 20),
        operation_id=posting.operation_id,
        journal_id=posting.journal_id,
    )
    return header, member


def pending(connection, header, member):
    connection.execute(insert(BusinessCredit).values(**header))
    connection.execute(insert(BusinessCreditLine).values(**member))


def test_unvalidated_credit_cannot_commit_and_notifications_roll_back(drafts):
    s = drafts
    header, member = credit_rows(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        pending(connection, header, member)
        assert set(
            connection.scalars(
                select(ChangeLog.entity_type).where(
                    ChangeLog.entity_id.in_([str(header["id"]), str(member["id"])])
                )
            )
        ) == {"business_credits", "business_credit_lines"}
        connection.exec_driver_sql("SET CONSTRAINTS tr_business_credits_complete IMMEDIATE")
    assert failure.value.orig.diag.constraint_name == "ck_business_credit_shape"
    assert snapshot(s) == before


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_credit_writes_require_fresh_post_lock_snapshots(drafts, isolation):
    s = drafts
    header, member = credit_rows(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL " + isolation)
        pending(connection, header, member)
    assert failure.value.orig.diag.constraint_name == "ck_business_credit_isolation"
    assert snapshot(s) == before


@pytest.mark.parametrize("table", [BusinessCredit, BusinessCreditLine])
@pytest.mark.parametrize("action", ["update", "delete"])
def test_credit_groups_and_members_are_immutable_even_before_commit(drafts, table, action):
    s = drafts
    header, member = credit_rows(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        pending(connection, header, member)
        statement = update(table).values(version=1) if action == "update" else delete(table)
        connection.execute(statement)
    assert failure.value.orig.diag.constraint_name == "ck_business_credit_immutable"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("net_amount", Decimal("-1")),
        ("tax_amount", Decimal("-1")),
        ("total_amount", Decimal("0")),
        ("total_amount", Decimal("19.99")),
    ],
)
def test_manual_components_have_exact_nonnegative_database_bounds(drafts, field, value):
    s = drafts
    header, member = credit_rows(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        pending(connection, header, member | {field: value})
    assert failure.value.orig.diag.constraint_name == "ck_business_credit_line_amounts"
    assert snapshot(s) == before


def test_storage_overflow_is_rejected_before_numeric_rounding(drafts):
    s = drafts
    header, member = credit_rows(s)
    before = snapshot(s)
    with pytest.raises(DataError) as failure, s["engine"].begin() as connection:
        pending(connection, header, member | {"total_amount": Decimal("100000000000000000000")})
    assert failure.value.orig.sqlstate == "22003"
    assert snapshot(s) == before


def test_source_line_must_belong_to_the_exact_document(drafts):
    s = drafts
    header, member = credit_rows(s)
    other, _ = prepare(s)
    member["source_line_id"] = other.lines[0].id
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        pending(connection, header, member)
    assert failure.value.orig.diag.constraint_name == "fk_business_credit_line_source"
    assert snapshot(s) == before


def test_original_document_cannot_cross_owned_ledgers(drafts):
    s = drafts
    header, _ = credit_rows(s)
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional credit isolation",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        connection.execute(
            insert(BusinessCredit).values(**(header | {"ledger_id": other.ledger.id}))
        )
    assert failure.value.orig.diag.constraint_name == "fk_business_credit_document"
    assert snapshot(s) == before


def test_empty_credit_round_trip_and_historical_notifications_block_downgrade(drafts):
    s = drafts
    migration = credit_storage_migration()
    with (
        s["engine"].begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        migration["downgrade"]()
        migration["upgrade"]()
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        connection.exec_driver_sql(
            "CREATE TEMP TABLE business_credits(id uuid, ledger_id uuid, version int) "
            "ON COMMIT DROP; "
            "CREATE TRIGGER fictional_credit_history AFTER INSERT ON pg_temp.business_credits "
            "FOR EACH ROW EXECUTE FUNCTION public.coinpup_record_change()"
        )
        connection.execute(
            text("INSERT INTO pg_temp.business_credits VALUES (:id,:ledger,1)"),
            {"id": uuid4(), "ledger": s["ledger"]},
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration["downgrade"]()
    assert failure.value.orig.diag.constraint_name == "ck_business_credit_downgrade"
    assert snapshot(s) == before
