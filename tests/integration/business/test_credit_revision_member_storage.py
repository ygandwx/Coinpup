"""Rollback-only storage probes: no revision member is yet a committable financial fact."""

from datetime import date
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.credit_revision_member_models import (
    BusinessCreditRevisionMember as Member,
)
from coinpup_api.business.credit_revision_models import BusinessCreditRevision as Revision
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import DataError, IntegrityError

from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_credit_revision_storage import revision as revision
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import credit_revision_member_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def member(header, original, action="correct"):
    first = original.postings[0]
    row = dict(
        id=uuid4(),
        ledger_id=header["ledger_id"],
        revision_id=header["id"],
        credit_id=header["credit_id"],
        document_id=header["document_id"],
        member_no=1,
        action=action,
        source_line_id=first.source_line_id,
        source_operation_id=first.source_operation_id,
        operation_id=first.operation_id,
        operation_version=2,
        previous_line_no=1,
        previous_operation_version=1,
        previous_journal_id=first.journal_id,
        reversal_journal_id=uuid4(),
        line_no=1,
        journal_id=uuid4(),
        net_amount="1",
        tax_amount="0",
        total_amount="1",
        recognition_date=date(2026, 11, 6),
    )
    if action == "create":
        row.update(
            operation_version=1,
            previous_line_no=None,
            previous_operation_version=None,
            previous_journal_id=None,
            reversal_journal_id=None,
            journal_id=first.journal_id,
        )
    if action == "cancel":
        row.update(
            line_no=None,
            journal_id=None,
            net_amount=None,
            tax_amount=None,
            total_amount=None,
            recognition_date=None,
        )
    return row


@pytest.mark.parametrize("action", ["create", "correct", "cancel"])
@pytest.mark.parametrize("immediate", [False, True])
def test_member_and_header_gates_reject_complete_looking_rows(revision, action, immediate):
    s, header, _, original = revision
    row = member(header, original, action)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        connection.execute(insert(Revision).values(**header))
        connection.execute(insert(Member).values(**row))
        assert connection.execute(
            select(ChangeLog.entity_type, ChangeLog.entity_version).where(
                ChangeLog.entity_id == str(row["id"])
            )
        ).one() == ("business_credit_revision_members", 1)
        if immediate:
            connection.exec_driver_sql(
                "SET CONSTRAINTS tr_business_credit_revision_members_complete IMMEDIATE"
            )
    assert failure.value.orig.diag.constraint_name == "ck_credit_revision_shape"
    assert snapshot(s) == before


@pytest.mark.parametrize("action", ["update", "delete"])
def test_members_remain_immutable_in_the_creating_transaction(revision, action):
    s, header, _, original = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        connection.execute(insert(Revision).values(**header))
        connection.execute(insert(Member).values(**member(header, original)))
        connection.execute(
            update(Member).values(version=1) if action == "update" else delete(Member)
        )
    assert failure.value.orig.diag.constraint_name == "ck_credit_revision_immutable"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "action,changes,suffix",
    [
        ("create", {"member_no": 0}, "position"),
        ("create", {"member_no": 401}, "position"),
        ("create", {"action": "other"}, "action"),
        ("create", {"version": 2}, "version"),
        ("create", {"previous_line_no": 1}, "previous"),
        ("create", {"previous_journal_id": uuid4()}, "previous"),
        ("create", {"operation_version": 2}, "previous"),
        ("correct", {"previous_line_no": 0}, "previous"),
        ("correct", {"previous_line_no": 201}, "previous"),
        ("correct", {"previous_operation_version": None}, "previous"),
        ("correct", {"previous_operation_version": 0}, "previous"),
        ("correct", {"previous_operation_version": 2147483647}, "previous"),
        ("correct", {"operation_version": 3}, "previous"),
        ("correct", {"reversal_journal_id": None}, "previous"),
        ("correct", {"line_no": 201}, "replacement"),
        ("correct", {"journal_id": None}, "replacement"),
        ("correct", {"net_amount": None}, "replacement"),
        ("correct", {"tax_amount": "-1", "total_amount": "0"}, "replacement"),
        ("correct", {"total_amount": "2"}, "replacement"),
        ("correct", {"recognition_date": None}, "replacement"),
        ("cancel", {"line_no": 1}, "replacement"),
        ("cancel", {"net_amount": "0"}, "replacement"),
        ("cancel", {"journal_id": uuid4()}, "replacement"),
    ],
)
def test_transition_checks_reject_partial_or_ambiguous_members(revision, action, changes, suffix):
    s, header, _, original = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        connection.execute(insert(Revision).values(**header))
        connection.execute(insert(Member).values(**(member(header, original, action) | changes)))
    assert failure.value.orig.diag.constraint_name == "ck_credit_revision_member_" + suffix
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "field,suffix",
    [
        ("credit_id", "group"),
        ("document_id", "group"),
        ("ledger_id", "group"),
        ("source_line_id", "source"),
        ("source_operation_id", "original"),
        ("operation_id", "operation"),
        ("previous_journal_id", "previous"),
        ("reversal_journal_id", "reversal"),
        ("journal_id", "journal"),
    ],
)
def test_member_references_reject_cross_scope_or_unbound_history(revision, field, suffix):
    s, header, _, original = revision
    replacement = uuid4()
    if field == "ledger_id":
        replacement = (
            s["structure"]
            .create_entity(
                s["owner"],
                EntityCreate(
                    kind="personal",
                    name="Fictional other revision ledger",
                    base_asset_id="USD",
                    template_key="personal_default",
                ),
            )
            .ledger.id
        )
    row = member(header, original) | {field: replacement}
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        connection.execute(insert(Revision).values(**header))
        connection.execute(insert(Member).values(**row))
        connection.exec_driver_sql(
            "SET CONSTRAINTS fk_credit_revision_member_" + suffix + " IMMEDIATE"
        )
    assert failure.value.orig.diag.constraint_name == "fk_credit_revision_member_" + suffix
    assert snapshot(s) == before


def test_member_extremes_allow_distinct_versions_of_the_same_operation_before_the_gate(revision):
    s, header, _, original = revision
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        connection.execute(insert(Revision).values(**header))
        first = member(header, original) | dict(
            member_no=400,
            previous_line_no=200,
            line_no=200,
            previous_operation_version=2147483646,
            operation_version=2147483647,
        )
        connection.execute(insert(Member).values(**first))
        second_header = header | dict(
            id=uuid4(),
            group_version=3,
            previous_revision_id=header["id"],
            idempotency_key="fictional-next-revision",
        )
        connection.execute(insert(Revision).values(**second_header))
        second = member(second_header, original)
        connection.execute(insert(Member).values(**second))
        assert (
            connection.execute(select(Member.operation_id)).scalars().all()
            == [first["operation_id"]] * 2
        )
        transaction.rollback()  # Storage shape only; no invented financial history is committed.
    assert snapshot(s) == before


def test_duplicate_transition_is_rejected_before_the_commit_gate(revision):
    s, header, _, original = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        connection.execute(insert(Revision).values(**header))
        first = member(header, original)
        connection.execute(insert(Member).values(**first))
        # Repeating a whole row must reject at an existing unique key, never reach the commit gate.
        second = first | dict(id=uuid4(), journal_id=uuid4(), reversal_journal_id=uuid4())
        connection.execute(insert(Member).values(**second))
    assert failure.value.orig.diag.constraint_name.startswith("uq_credit_revision_member_")
    assert snapshot(s) == before


def test_member_amounts_keep_the_numeric_38_18_range(revision):
    s, header, _, original = revision
    before = snapshot(s)
    with pytest.raises(DataError) as failure, s["engine"].begin() as connection:
        connection.execute(insert(Revision).values(**header))
        row = member(header, original) | dict(
            net_amount="100000000000000000000", total_amount="100000000000000000000"
        )
        connection.execute(insert(Member).values(**row))
    assert failure.value.orig.sqlstate == "22003"
    assert snapshot(s) == before


def test_empty_member_schema_round_trip_preserves_header_gate_and_old_receipts(revision):
    s, _, request, original = revision
    before = snapshot(s)
    migration = credit_revision_member_migration()
    with (
        s["engine"].begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        old_definitions = definitions(connection)
        migration["downgrade"]()
        functions, _ = definitions(connection)
        assert functions == [
            body
            for body in old_definitions[0]
            if not any(
                name in body.split("\n", 1)[0]
                for name in (
                    "coinpup_check_credit_revision_member",
                    "coinpup_validate_credit_origin_facts",
                    "coinpup_validate_credit_group_transitions",
                    "coinpup_business_credit_revision_response",
                    "coinpup_validate_credit_revision_receipt",
                    "coinpup_validate_credit_posting_fact",
                    "coinpup_credit_posting_facts",
                    "coinpup_current_credit_lines",
                )
            )
        ]
        migration["upgrade"]()
        assert definitions(connection) == old_definitions
    assert credit(s, request) == original
    assert snapshot(s) == before


@pytest.mark.parametrize("history", ["header", "member", "notification", "group_notification"])
def test_member_history_or_notifications_refuse_downgrade(revision, history):
    s, header, _, original = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        if history in {"header", "member"}:
            connection.execute(insert(Revision).values(**header))
            if history == "member":
                connection.execute(insert(Member).values(**member(header, original)))
        else:
            table = (
                "business_credits"
                if history == "group_notification"
                else "business_credit_revision_members"
            )
            connection.exec_driver_sql(
                f"CREATE TEMP TABLE {table}(id uuid, ledger_id uuid, version int) ON COMMIT DROP; "
                f"CREATE TRIGGER fictional_member_history AFTER INSERT ON pg_temp.{table} "
                "FOR EACH ROW EXECUTE FUNCTION public.coinpup_record_change()"
            )
            connection.execute(
                text(f"INSERT INTO pg_temp.{table} VALUES (:id,:ledger,:version)"),
                dict(
                    id=uuid4(),
                    ledger=s["ledger"],
                    version=2 if history == "group_notification" else 1,
                ),
            )
        with Operations.context(MigrationContext.configure(connection)):
            credit_revision_member_migration()["downgrade"]()
    assert failure.value.orig.diag.constraint_name == "ck_credit_revision_downgrade"
    assert snapshot(s) == before
