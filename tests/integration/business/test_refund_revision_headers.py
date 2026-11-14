"""Exercise the closed refund write gate against a real, already receipted refund."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.refund_revision_models import BusinessRefundRevision as Revision
from coinpup_api.ledger.models import Journal
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_service import refund, setup_refund
from tests.integration.conftest import (
    _restore_current_change_types,
    refund_revision_header_migration,
)
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.fixture
def revision(drafts):
    return refund_header(drafts, allocated=False)


def refund_header(drafts, *, allocated):
    payment, _, payload = setup_refund(drafts, allocated=allocated)
    original = refund(drafts, payload)
    header = dict(
        id=uuid4(),
        ledger_id=drafts["ledger"],
        created_by=drafts["owner"],
        refund_id=original.id,
        payment_id=payment.id,
        operation_id=original.operation_id,
        operation_version=2,
        previous_journal_id=original.journal_id,
        reversal_journal_id=uuid4(),
        action="cancel",
        revision_reason="Fictional refund correction",
        idempotency_key="fictional-refund",
    )
    return drafts, header, payload, original


def replacement(payload):
    return dict(
        action="correct",
        replacement_journal_id=uuid4(),
        account_id=payload.account_id,
        asset_id=payload.asset_id,
        amount=Decimal("19.99"),
        payment_amount=Decimal("19.99"),
        reason="Fictional replacement",
        fees=[],
        transaction_date=date(2026, 10, 3),
    )


@pytest.mark.parametrize("correct", [False, True])
def test_closed_gate_and_original_replay(revision, correct):
    s, header, payload, original = revision
    if correct:
        header.update(replacement(payload))
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**header))
        assert c.execute(
            select(ChangeLog.entity_type, ChangeLog.entity_version).where(
                ChangeLog.entity_id == str(header["id"])
            )
        ).one() == ("business_refund_revisions", 1)
        c.exec_driver_sql("SET CONSTRAINTS tr_business_refund_revisions_complete IMMEDIATE")
    assert caught.value.orig.diag.constraint_name == "ck_refund_revision_shape"
    assert refund(s, payload) == original
    assert snapshot(s) == before


def test_deferred_gate_rejects_commit_with_all_journal_ids_present(revision):
    s, header, _, _ = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**header))
        previous = (
            c.execute(select(Journal.__table__).where(Journal.id == header["previous_journal_id"]))
            .mappings()
            .one()
        )
        c.execute(
            insert(Journal).values(
                id=header["reversal_journal_id"],
                operation_id=header["operation_id"],
                ledger_id=s["ledger"],
                operation_version=2,
                journal_kind="reversal",
                reverses_journal_id=previous["id"],
                transaction_date=previous["transaction_date"],
                recognition_date=previous["recognition_date"],
                description=previous["description"],
                revision_reason=header["revision_reason"],
            )
        )
    assert caught.value.orig.diag.constraint_name == "ck_refund_revision_shape"
    assert snapshot(s) == before


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_revision_insert_requires_read_committed(revision, isolation):
    s, header, _, _ = revision
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL " + isolation)
        c.execute(insert(Revision).values(**header))
    assert caught.value.orig.diag.constraint_name == "ck_refund_revision_isolation"


@pytest.mark.parametrize("field", ["ledger_id", "payment_id", "operation_id", "refund_id"])
def test_header_cannot_link_another_refund_or_ledger(revision, field):
    s, header, _, _ = revision
    if field == "ledger_id":
        other = s["structure"].create_entity(
            s["owner"],
            EntityCreate(
                kind="personal",
                name="Fictional isolated ledger",
                base_asset_id="USD",
                template_key="personal_default",
            ),
        )
        header[field] = other.ledger.id
    else:
        header[field] = uuid4()
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**header))
    assert caught.value.orig.diag.constraint_name == "fk_refund_revision_original"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"operation_version": 1}, "predecessor"),
        ({"operation_version": 3}, "predecessor"),
        ({"revision_reason": "\t"}, "reason"),
        ({"revision_reason": " leading"}, "reason"),
        ({"amount": None}, "replacement"),
        ({"action": "cancel"}, "replacement"),
        ({"payment_amount": Decimal("0")}, "amounts"),
        ({"amount": Decimal("-0.01")}, "amounts"),
        ({"fees": {}}, "fees"),
        ({"fees": [{}] * 21}, "fees"),
        ({"reason": "\n"}, "fact_reason"),
    ],
)
def test_correction_rejects_partial_facts(revision, changes, constraint):
    s, header, payload, _ = revision
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**(header | replacement(payload) | changes)))
    assert caught.value.orig.diag.constraint_name == "ck_refund_revision_" + constraint


@pytest.mark.parametrize("command", ["update", "delete", "journal", "predecessor", "downgrade"])
def test_invalid_pending_history_cannot_escape_the_transaction(revision, command):
    s, header, _, _ = revision
    before = snapshot(s)
    errors = {
        "update": "ck_refund_revision_immutable",
        "delete": "ck_refund_revision_immutable",
        "journal": "fk_refund_revision_reversal_journal",
        "predecessor": "fk_refund_revision_predecessor",
        "downgrade": "ck_refund_revision_downgrade",
    }
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**header))
        if command == "update":
            c.execute(update(Revision).where(Revision.id == header["id"]).values(version=1))
        elif command == "delete":
            c.execute(delete(Revision).where(Revision.id == header["id"]))
        elif command == "journal":
            c.exec_driver_sql("SET CONSTRAINTS fk_refund_revision_reversal_journal IMMEDIATE")
        elif command == "predecessor":
            c.execute(
                insert(Revision).values(
                    **(
                        header
                        | dict(
                            id=uuid4(),
                            operation_version=3,
                            previous_revision_id=uuid4(),
                            reversal_journal_id=uuid4(),
                            idempotency_key="fictional-next",
                        )
                    )
                )
            )
        else:
            with Operations.context(MigrationContext.configure(c)):
                refund_revision_header_migration(include_dependents=False)["downgrade"]()
    assert caught.value.orig.diag.constraint_name == errors[command]
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "entity_type,version", [("business_refunds", 2), ("business_refund_revisions", 1)]
)
def test_notification_history_alone_blocks_downgrade(revision, entity_type, version):
    s, header, _, _ = revision
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.exec_driver_sql(
            f"CREATE TEMP TABLE {entity_type}(id uuid,ledger_id uuid,version int) ON COMMIT DROP"
        )
        c.exec_driver_sql(
            f"CREATE TRIGGER fictional_refund_history AFTER INSERT ON pg_temp.{entity_type} "
            "FOR EACH ROW EXECUTE FUNCTION public.coinpup_record_change()"
        )
        c.execute(
            text(f"INSERT INTO pg_temp.{entity_type} VALUES (:id, :ledger, :version)"),
            {"id": header["id"], "ledger": s["ledger"], "version": version},
        )
        with Operations.context(MigrationContext.configure(c)):
            refund_revision_header_migration(include_dependents=False)["downgrade"]()
    assert caught.value.orig.diag.constraint_name == "ck_refund_revision_downgrade"


def schema_catalog(connection):
    return connection.exec_driver_sql(
        "SELECT 'constraint:' || conrelid::regclass::text || ':' || conname, "
        "pg_get_constraintdef(oid) FROM pg_constraint WHERE connamespace='public'::regnamespace "
        "UNION ALL SELECT 'function:' || proname || ':' || "
        "pg_get_function_identity_arguments(oid), "
        "pg_get_functiondef(oid) FROM pg_proc WHERE pronamespace='public'::regnamespace "
        "AND proname LIKE 'coinpup_%' "
        "UNION ALL SELECT 'trigger:' || tgrelid::regclass::text || ':' || tgname, "
        "tgenabled::text || ':' || pg_get_triggerdef(oid) FROM pg_trigger "
        "WHERE NOT tgisinternal ORDER BY 1",
        execution_options={"no_parameters": True},
    ).all()


def test_no_revisions_allows_round_trip_and_old_201_replay(revision):
    s, _, payload, original = revision
    before = snapshot(s)
    migration = refund_revision_header_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        catalog = schema_catalog(c)
        migration["downgrade"]()
        assert (
            c.exec_driver_sql("SELECT to_regclass('public.business_refund_revisions')").scalar()
            is None
        )
        migration["upgrade"]()
        # The frozen round trip reinstates its historical notification list. Use
        # the existing current-type fixture and prove the complete catalog unchanged;
        # re-parsing pg_get_constraintdef itself changes PostgreSQL's cast syntax.
        _restore_current_change_types(c)
        assert schema_catalog(c) == catalog
    assert refund(s, payload) == original
    assert snapshot(s) == before
