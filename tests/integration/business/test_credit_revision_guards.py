"""Revision notifications and ownership checks do not open the existing financial gate."""

from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.credit_revision_models import BusinessCreditRevision as Revision
from coinpup_api.ledger.models import FinancialOperation
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.credit_transition_fixture import group, transition
from tests.integration.business.test_credit_fact_validation import copy_posting, lock_credit, stored
from tests.integration.business.test_credit_revision_storage import (
    definitions,
)
from tests.integration.business.test_credit_revision_storage import (
    revision as revision,
)
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import credit_revision_guards_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("action", ["correct", "cancel"])
def test_stable_group_events_preserve_own_version_one_notifications_and_the_closed_gate(
    revision, action
):
    s, header, _, original = revision
    before = snapshot(s)
    second = header | dict(
        id=uuid4(),
        group_version=3,
        previous_revision_id=header["id"],
        idempotency_key="fictional-second-revision",
        action=action,
    )
    if action == "cancel":
        second |= dict(credit_date=None, credit_reason=None)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        connection.execute(insert(Revision), [header, second])
        events = (
            connection.execute(
                select(ChangeLog.__table__)
                .where(
                    ChangeLog.entity_type == "business_credits",
                    ChangeLog.entity_id == str(original.id),
                )
                .order_by(ChangeLog.entity_version)
            )
            .mappings()
            .all()
        )
        assert [(row["entity_version"], row["change_kind"]) for row in events] == [
            (1, "upsert"),
            (2, "upsert"),
            (3, "cancel" if action == "cancel" else "upsert"),
        ]
        assert all(
            row["owner_id"] == s["owner"] and row["ledger_id"] == s["ledger"] for row in events
        )
        own = connection.execute(
            select(ChangeLog.entity_id, ChangeLog.entity_version).where(
                ChangeLog.entity_type == "business_credit_revisions",
            )
        ).all()
        assert set(own) == {(str(header["id"]), 1), (str(second["id"]), 1)}
        assert (
            connection.scalar(
                text("SELECT version FROM business_credits WHERE id=:id"), {"id": original.id}
            )
            == 1
        )
        connection.exec_driver_sql(
            "SET CONSTRAINTS tr_business_credit_revisions_complete IMMEDIATE"
        )
        pytest.fail("Stable notifications must not open revision writes")
    assert failure.value.orig.diag.constraint_name == "ck_credit_revision_shape"
    assert (
        failure.value.orig.diag.message_primary
        == "Credit revision requires complete group validation"
    )
    assert snapshot(s) == before


@pytest.mark.parametrize("mode", ["unowned", "head_only", "with_reversal"])
def test_target_operation_trigger_rejects_missing_owner_or_current_v2_with_original_contract(
    revision, mode
):
    s, _, _, original = revision
    prior = original.postings[0]
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        lock_credit(connection, original)
        if mode == "unowned":
            connection.execute(
                insert(FinancialOperation).values(
                    **(
                        stored(connection, FinancialOperation, prior.operation_id)
                        | dict(id=uuid4())
                    )
                )
            )
        else:
            connection.execute(
                update(FinancialOperation)
                .where(FinancialOperation.id == prior.operation_id)
                .values(version=2, status="cancelled")
            )
            if mode == "with_reversal":
                copy_posting(
                    connection,
                    prior.journal_id,
                    dict(
                        id=uuid4(),
                        operation_version=2,
                        journal_kind="reversal",
                        reverses_journal_id=prior.journal_id,
                        revision_reason="Fictional isolated cancellation",
                    ),
                    reverse=True,
                )
        connection.exec_driver_sql("SET CONSTRAINTS zz_tr_credit_operations_complete IMMEDIATE")
        pytest.fail("The operation ownership trigger did not reject the incomplete state")
    assert failure.value.orig.diag.constraint_name == (
        "ck_credit_revision_shape" if mode == "unowned" else "ck_business_credit_shape"
    )
    assert failure.value.orig.diag.message_primary == (
        "Credit operation ownership is invalid"
        if mode == "unowned"
        else "Business credit line shape is invalid"
    )
    assert snapshot(s) == before


def test_empty_guard_roundtrip_preserves_every_old_function_trigger_and_original_receipt(revision):
    s, _, request, original = revision
    migration = credit_revision_guards_migration()
    before = snapshot(s)
    with (
        s["engine"].begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        current = definitions(connection)
        row = connection.execute(
            text(
                "SELECT tgdeferrable,tginitdeferred,tgenabled "
                "FROM pg_trigger WHERE tgname='zz_tr_credit_operations_complete' "
                "AND tgrelid='public.financial_operations'::regclass"
            )
        ).one()
        assert row == (True, True, "O")
        migration["downgrade"]()
        old = definitions(connection)
        assert old[0] == [
            body
            for body in current[0]
            if not any(
                name in body.split("\n", 1)[0]
                for name in (
                    "coinpup_check_credit_operation_group",
                    "coinpup_record_credit_group_change",
                )
            )
        ]
        assert old[1] == [
            body
            for body in current[1]
            if not any(
                name in body
                for name in (
                    "zz_tr_credit_operations_complete",
                    "trg_business_credit_group_change_log",
                )
            )
        ]
        migration["upgrade"]()
        assert definitions(connection) == current
        connection.execute(
            text("SELECT coinpup_validate_business_credit(:id)"), {"id": original.id}
        )
    assert credit(s, request) == original
    assert snapshot(s) == before


def test_operation_owned_only_by_new_member_resolves_its_group_before_existing_v1_guard(drafts):
    s = drafts
    state = group(s, count=2, selected=1)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        transition(connection, state, [state["source_ids"][1]])
        connection.exec_driver_sql("SET CONSTRAINTS zz_tr_credit_operations_complete IMMEDIATE")
        pytest.fail("The current v1 business validator was bypassed")
    assert failure.value.orig.diag.constraint_name == "ck_business_credit_shape"
    assert failure.value.orig.diag.message_primary == "Business credit line shape is invalid"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "history",
    ["header", "business_credits", "business_credit_revisions", "business_credit_revision_members"],
)
def test_guard_downgrade_refuses_revision_or_notifications_even_without_business_rows(
    revision, history
):
    s, header, _, original = revision
    before = snapshot(s)
    with (
        pytest.raises(IntegrityError) as failure,
        s["engine"].begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        if history == "header":
            connection.execute(insert(Revision).values(**header))
        else:
            connection.exec_driver_sql(
                f"CREATE TEMP TABLE {history}(id uuid,ledger_id uuid,version integer) "
                "ON COMMIT DROP"
            )
            connection.exec_driver_sql(
                f"CREATE TRIGGER fictional_guard_history AFTER INSERT ON pg_temp.{history} "
                "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
            )
            connection.execute(
                text(f"INSERT INTO pg_temp.{history} VALUES(:id,:ledger,:version)"),
                dict(
                    id=original.id,
                    ledger=s["ledger"],
                    version=2 if history == "business_credits" else 1,
                ),
            )
        credit_revision_guards_migration()["downgrade"]()
        pytest.fail("Protected revision history was removed")
    assert failure.value.orig.diag.constraint_name == "ck_credit_revision_downgrade"
    assert snapshot(s) == before
