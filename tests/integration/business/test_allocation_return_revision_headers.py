"""Frozen return headers cannot advance money or a group before complete member validation."""

from datetime import date
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.allocation_return_revision_models import (
    BusinessAllocationReturnRevision as Revision,
)
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import return_revision_header_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.fixture
def revision(drafts):
    s = drafts
    payment, _, _, body = setup_return(s)
    original = submit_return(s, body)
    data = dict(
        id=uuid4(),
        return_id=original.id,
        payment_id=payment.id,
        ledger_id=s["ledger"],
        group_version=2,
        previous_revision_id=None,
        action="correct",
        revision_reason="Fictional explicit correction",
        return_date=date(2026, 11, 8),
        return_reason="Fictional replacement",
        created_by=s["owner"],
        idempotency_key="fictional-return",
    )
    return s, data, body, original


@pytest.mark.parametrize("action", ["correct", "cancel"])
@pytest.mark.parametrize("explicit", [False, True])
def test_closed_gate_rolls_back_entire_return_revision_and_notification(revision, action, explicit):
    s, data, body, original = revision
    if action == "cancel":
        data.update(action=action, return_date=None, return_reason=None)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**data))
        assert c.execute(
            select(ChangeLog.entity_type, ChangeLog.entity_version).where(
                ChangeLog.entity_id == str(data["id"])
            )
        ).one() == ("business_return_revisions", 1)
        if explicit:
            c.exec_driver_sql("SET CONSTRAINTS tr_business_return_revisions_complete IMMEDIATE")
    assert caught.value.orig.diag.constraint_name == "ck_return_revision_shape"
    assert submit_return(s, body) == original and snapshot(s) == before


@pytest.mark.parametrize(
    "change,constraint",
    [
        ({"payment_id": uuid4()}, "fk_business_return_revision_group"),
        ({"return_id": uuid4()}, "fk_business_return_revision_group"),
        ({"created_by": uuid4()}, "fk_business_return_revision_owner"),
        ({"group_version": 1}, "ck_business_return_revision_group_version"),
        ({"group_version": 3}, "ck_business_return_revision_previous"),
        (
            {"group_version": 3, "previous_revision_id": uuid4()},
            "fk_business_return_revision_previous",
        ),
        ({"action": "cancel"}, "ck_business_return_revision_replacement"),
        ({"action": "invalid"}, "ck_business_return_revision_action"),
        ({"return_date": None}, "ck_business_return_revision_replacement"),
        ({"revision_reason": "\tbad"}, "ck_business_return_revision_reason"),
        ({"return_reason": "bad\n"}, "ck_business_return_revision_replacement"),
        ({"version": 2}, "ck_business_return_revision_version"),
    ],
)
def test_invalid_scope_predecessor_or_replacement_rolls_back(revision, change, constraint):
    s, data, _, _ = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**(data | change)))
    assert caught.value.orig.diag.constraint_name == constraint and snapshot(s) == before


@pytest.mark.parametrize("command", ["update", "delete", "isolation", "downgrade", "group", "key"])
def test_immutable_headers_require_locking_constraints_and_preserve_history(revision, command):
    s, data, _, _ = revision
    before = snapshot(s)
    errors = {
        "update": "ck_return_revision_immutable",
        "delete": "ck_return_revision_immutable",
        "isolation": "ck_return_revision_isolation",
        "downgrade": "ck_return_revision_downgrade",
        "group": "uq_business_return_revision_group_version",
        "key": "uq_business_return_revision_key",
    }
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        if command == "isolation":
            c.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        c.execute(insert(Revision).values(**data))
        if command == "update":
            c.execute(update(Revision).values(version=1))
        if command == "delete":
            c.execute(delete(Revision))
        if command == "downgrade":
            with Operations.context(MigrationContext.configure(c)):
                return_revision_header_migration()["downgrade"]()
        if command in {"key", "group"}:
            changes = (
                {"idempotency_key": "fictional-second"}
                if command == "group"
                else {"group_version": 3, "previous_revision_id": data["id"]}
            )
            c.execute(insert(Revision).values(**(data | changes | {"id": uuid4()})))
    assert caught.value.orig.diag.constraint_name == errors[command] and snapshot(s) == before


@pytest.mark.parametrize(
    "table,version", [("business_return_revisions", 1), ("business_allocation_returns", 2)]
)
def test_notification_history_prevents_downgrade_without_rows(revision, table, version):
    s, data, _, _ = revision
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.exec_driver_sql(
            f"CREATE TEMP TABLE {table}(id uuid, ledger_id uuid, version int) ON COMMIT DROP"
        )
        c.exec_driver_sql(
            f"CREATE TRIGGER fictional_return_history AFTER INSERT ON pg_temp.{table} "
            "FOR EACH ROW EXECUTE FUNCTION public.coinpup_record_change()"
        )
        c.execute(
            text(f"INSERT INTO pg_temp.{table} VALUES (:id, :ledger, :version)"),
            {"id": data["id"], "ledger": s["ledger"], "version": version},
        )
        with Operations.context(MigrationContext.configure(c)):
            return_revision_header_migration()["downgrade"]()
    assert caught.value.orig.diag.constraint_name == "ck_return_revision_downgrade"


def test_empty_roundtrip_preserves_original_receipt_and_existing_db_functions(revision):
    s, _, body, original = revision
    before = snapshot(s)
    migration = return_revision_header_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        original_schema = definitions(c)
        migration["downgrade"]()
        assert (
            c.exec_driver_sql("SELECT to_regclass('public.business_return_revisions')").scalar()
            is None
        )
        migration["upgrade"]()
        assert definitions(c) == original_schema
    assert submit_return(s, body) == original and snapshot(s) == before
