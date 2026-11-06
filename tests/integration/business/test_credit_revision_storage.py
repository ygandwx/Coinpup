"""Fictional gated headers cannot change old credit facts or leak partial revision notifications."""

from datetime import date
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.credit_revision_models import BusinessCreditRevision as Revision
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_credit_service import credit, setup_credit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import credit_revision_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.fixture
def revision(drafts):
    s = drafts
    document, _, _, request = setup_credit(s)
    original = credit(s, request)
    header = dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        credit_id=original.id,
        document_id=document.id,
        group_version=2,
        previous_revision_id=None,
        action="correct",
        revision_reason="Fictional revision reason 虚构更正",
        credit_date=date(2026, 11, 6),
        credit_reason="Fictional replacement",
        created_by=s["owner"],
        idempotency_key="fictional-credit",
    )
    # Reuse an existing FK only in this rollback-only storage probe; no revision can commit.
    return s, header, request, original


@pytest.mark.parametrize("immediate", [False, True])
@pytest.mark.parametrize("action", ["correct", "cancel"])
def test_gated_revision_cannot_commit_or_leave_notifications(revision, immediate, action):
    s, header, _, _ = revision
    if action == "cancel":
        header = header | dict(action=action, credit_date=None, credit_reason=None)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        connection.execute(insert(Revision).values(**header))
        assert connection.execute(
            select(ChangeLog.entity_type, ChangeLog.entity_version).where(
                ChangeLog.entity_id == str(header["id"])
            )
        ).one() == ("business_credit_revisions", 1)
        if immediate:
            connection.exec_driver_sql(
                "SET CONSTRAINTS tr_business_credit_revisions_complete IMMEDIATE"
            )
    assert failure.value.orig.diag.constraint_name == "ck_credit_revision_shape"
    assert snapshot(s) == before


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_revision_insert_requires_read_committed(revision, isolation):
    s, header, _, _ = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL " + isolation)
        connection.execute(insert(Revision).values(**header))
    assert failure.value.orig.diag.constraint_name == "ck_credit_revision_isolation"
    assert snapshot(s) == before


@pytest.mark.parametrize("action", ["update", "delete"])
def test_revision_headers_are_immutable_even_before_commit(revision, action):
    s, header, _, _ = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        connection.execute(insert(Revision).values(**header))
        connection.execute(
            update(Revision).values(version=1) if action == "update" else delete(Revision)
        )
    assert failure.value.orig.diag.constraint_name == "ck_credit_revision_immutable"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "changes,suffix",
    [
        ({"version": 2}, "version"),
        ({"group_version": 1}, "group_version"),
        ({"group_version": 3}, "previous"),
        ({"group_version": 3, "previous_revision_id": "self"}, "previous"),
        ({"previous_revision_id": uuid4()}, "previous"),
        ({"action": "other"}, "action"),
        ({"revision_reason": " "}, "reason"),
        ({"revision_reason": "\t"}, "reason"),
        ({"revision_reason": " padded"}, "reason"),
        ({"credit_date": None}, "replacement"),
        ({"credit_reason": None}, "replacement"),
        ({"credit_reason": " "}, "replacement"),
        ({"action": "cancel"}, "replacement"),
    ],
)
def test_header_shape_checks_reject_partial_or_ambiguous_facts(revision, changes, suffix):
    s, header, _, _ = revision
    if changes.get("previous_revision_id") == "self":
        changes = changes | {"previous_revision_id": header["id"]}
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        connection.execute(insert(Revision).values(**(header | changes)))
    assert failure.value.orig.diag.constraint_name == "ck_business_credit_revision_" + suffix
    assert snapshot(s) == before


@pytest.mark.parametrize("field", ["ledger", "document", "owner", "previous", "previous_group"])
def test_header_references_remain_in_the_same_ledger_and_group(revision, field):
    s, header, request, _ = revision
    if field == "ledger":
        other = s["structure"].create_entity(
            s["owner"],
            EntityCreate(
                kind="personal",
                name="Fictional isolated revision",
                base_asset_id="USD",
                template_key="personal_default",
            ),
        )
        changes, suffix = {"ledger_id": other.ledger.id}, "group"
    elif field == "document":
        changes, suffix = {"document_id": uuid4()}, "group"
    elif field == "owner":
        changes, suffix = {"created_by": uuid4()}, "owner"
    elif field == "previous_group":
        other = credit(s, request.model_copy(update={"id": uuid4()}), key="fictional-other-credit")
        changes, suffix = (
            dict(
                id=uuid4(),
                credit_id=other.id,
                document_id=header["document_id"],
                group_version=3,
                previous_revision_id=header["id"],
                idempotency_key="fictional-other-credit",
            ),
            "previous",
        )
    else:
        changes, suffix = {"group_version": 3, "previous_revision_id": uuid4()}, "previous"
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        if field == "previous_group":
            connection.execute(insert(Revision).values(**header))
        connection.execute(insert(Revision).values(**(header | changes)))
    assert failure.value.orig.diag.constraint_name == "fk_business_credit_revision_" + suffix
    assert snapshot(s) == before


@pytest.mark.parametrize("duplicate", ["group_version", "key"])
def test_revision_group_version_and_ledger_key_are_unique(revision, duplicate):
    s, header, _, _ = revision
    second = header | {"id": uuid4()}
    if duplicate == "group_version":
        second["idempotency_key"] = "fictional-other-revision"
    else:
        second.update(group_version=3, previous_revision_id=header["id"])
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        connection.execute(insert(Revision).values(**header))
        connection.execute(insert(Revision).values(**second))
    assert failure.value.orig.diag.constraint_name == "uq_business_credit_revision_" + duplicate
    assert snapshot(s) == before


def definitions(connection):
    return connection.execute(
        text(
            "SELECT pg_get_functiondef(p.oid) FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='public' AND p.proname LIKE :prefix ORDER BY p.proname,p.proargtypes"
        ),
        {"prefix": "coinpup_%"},
    ).scalars().all(), connection.exec_driver_sql(
        "SELECT pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND NOT t.tgisinternal "
        "ORDER BY c.relname,t.tgname"
    ).scalars().all()


def test_empty_revision_round_trip_preserves_existing_credit_receipts_and_financial_sql(revision):
    s, _, request, original = revision
    before = snapshot(s)
    migration = credit_revision_migration()
    with (
        s["engine"].begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        functions, triggers = definitions(connection)
        migration["downgrade"]()
        remaining, _ = definitions(connection)
        assert remaining == [
            definition
            for definition in functions
            if not any(
                name in definition.split("\n", 1)[0]
                for name in (
                    "coinpup_guard_credit_revision",
                    "coinpup_validate_credit_revision",
                    "coinpup_check_credit_revision",
                )
            )
        ]
        migration["upgrade"]()
        assert definitions(connection) == (functions, triggers)
    assert credit(s, request) == original
    assert snapshot(s) == before


def test_pending_revision_history_blocks_downgrade(revision):
    s, header, _, _ = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        connection.execute(insert(Revision).values(**header))
        with Operations.context(MigrationContext.configure(connection)):
            credit_revision_migration()["downgrade"]()
    assert failure.value.orig.diag.constraint_name == "ck_credit_revision_downgrade"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "table,version", [("business_credit_revisions", 1), ("business_credits", 2)]
)
def test_notification_only_history_blocks_downgrade(revision, table, version):
    s, _, _, _ = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        connection.exec_driver_sql(
            f"CREATE TEMP TABLE {table}(id uuid, ledger_id uuid, version int) ON COMMIT DROP; "
            f"CREATE TRIGGER fictional_revision_history AFTER INSERT ON pg_temp.{table} "
            "FOR EACH ROW EXECUTE FUNCTION public.coinpup_record_change()"
        )
        connection.execute(
            text(f"INSERT INTO pg_temp.{table} VALUES (:id,:ledger,:version)"),
            {"id": uuid4(), "ledger": s["ledger"], "version": version},
        )
        with Operations.context(MigrationContext.configure(connection)):
            credit_revision_migration()["downgrade"]()
    assert failure.value.orig.diag.constraint_name == "ck_credit_revision_downgrade"
    assert snapshot(s) == before
