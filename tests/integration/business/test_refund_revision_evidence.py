"""An immutable, ordered reference can name only a return of the fixed payment."""

from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.refund_revision_models import BusinessRefundRevision as Revision
from coinpup_api.business.refund_revision_return_models import (
    BusinessRefundRevisionReturn as Evidence,
)
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_headers import refund_header, replacement
from tests.integration.business.test_refund_service import refund
from tests.integration.business.test_refund_storage import another_payment
from tests.integration.conftest import refund_revision_evidence_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.fixture
def evidence(drafts):
    s, header, original_command, original = refund_header(drafts, allocated=True)
    row = dict(
        id=uuid4(),
        revision_id=header["id"],
        ledger_id=s["ledger"],
        refund_id=original.id,
        payment_id=original.payment_id,
        return_id=original_command.allocation_return_ids[0],
        return_version=1,
        line_no=1,
    )
    return s, header, original_command, original, row


@pytest.mark.parametrize(
    "case,expected",
    [
        ("correct", "ck_refund_revision_shape"),
        ("cancel", "ck_refund_revision_evidence"),
        ("gap", "ck_refund_revision_evidence"),
        ("update", "ck_refund_revision_immutable"),
        ("delete", "ck_refund_revision_immutable"),
        ("repeat", "uq_refund_revision_return_source"),
        ("downgrade", "ck_refund_revision_downgrade"),
    ],
)
def test_staged_evidence_is_ordered_and_immutable_and_leaves_no_writes(evidence, case, expected):
    s, header, command, original, row = evidence
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        if case != "cancel":
            header.update(replacement(command))
        if case == "gap":
            row["line_no"] = 2
        c.execute(insert(Revision).values(**header))
        c.execute(insert(Evidence).values(**row))
        assert c.execute(
            select(ChangeLog.entity_type, ChangeLog.entity_version).where(
                ChangeLog.entity_id == str(row["id"])
            )
        ).one() == ("business_refund_revision_returns", 1)
        if case == "update":
            c.execute(update(Evidence).where(Evidence.id == row["id"]).values(line_no=2))
        elif case == "delete":
            c.execute(delete(Evidence).where(Evidence.id == row["id"]))
        elif case == "repeat":
            c.execute(insert(Evidence).values(**(row | dict(id=uuid4(), line_no=2))))
        elif case == "downgrade":
            with Operations.context(MigrationContext.configure(c)):
                refund_revision_evidence_migration()["downgrade"]()
        else:
            c.exec_driver_sql("SET CONSTRAINTS tr_refund_revision_returns_complete IMMEDIATE")
    assert caught.value.orig.diag.constraint_name == expected
    assert refund(s, command) == original
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("line_no", 0, "ck_refund_revision_return_position"),
        ("line_no", 201, "ck_refund_revision_return_position"),
        ("version", 2, "ck_refund_revision_return_version"),
        ("return_version", 2, "ck_refund_revision_return_source_version"),
        ("revision_id", None, "fk_refund_revision_return_parent"),
        ("refund_id", None, "fk_refund_revision_return_parent"),
        ("return_id", None, "fk_refund_revision_return_source"),
    ],
)
def test_missing_or_mismatched_references_are_rejected(evidence, field, value, expected):
    s, header, command, _, row = evidence
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**(header | replacement(command))))
        c.execute(insert(Evidence).values(**(row | {field: uuid4() if value is None else value})))
    assert caught.value.orig.diag.constraint_name == expected
    assert snapshot(s) == before


def test_an_existing_return_of_a_different_payment_cannot_support_the_replacement(evidence):
    s, header, command, _, row = evidence
    other_payment = another_payment(s, s["ledger"], s["account"].id, s["party"].id)
    other_command = command.model_copy(
        update={
            "id": uuid4(),
            "payment_id": other_payment.id,
            "allocation_return_ids": [],
        }
    )
    other = refund(s, other_command, key="fictional-other-refund")
    header.update(
        refund_id=other.id,
        payment_id=other_payment.id,
        operation_id=other.operation_id,
        previous_journal_id=other.journal_id,
        idempotency_key="fictional-other-refund",
    )
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**(header | replacement(other_command))))
        c.execute(
            insert(Evidence).values(**(row | dict(refund_id=other.id, payment_id=other_payment.id)))
        )
    assert caught.value.orig.diag.constraint_name == "fk_refund_revision_return_source"
    assert snapshot(s) == before


def test_evidence_notification_alone_blocks_downgrade(evidence):
    s, _, _, _, row = evidence
    table = "business_refund_revision_returns"
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.exec_driver_sql(
            f"CREATE TEMP TABLE {table}(id uuid,ledger_id uuid,version int) ON COMMIT DROP"
        )
        c.exec_driver_sql(
            f"CREATE TRIGGER fictional_refund_return_history AFTER INSERT ON pg_temp.{table} "
            "FOR EACH ROW EXECUTE FUNCTION public.coinpup_record_change()"
        )
        c.execute(
            text(f"INSERT INTO pg_temp.{table} VALUES (:id,:ledger,1)"),
            {"id": row["id"], "ledger": s["ledger"]},
        )
        with Operations.context(MigrationContext.configure(c)):
            refund_revision_evidence_migration()["downgrade"]()
    assert caught.value.orig.diag.constraint_name == "ck_refund_revision_downgrade"
    assert snapshot(s) == before
