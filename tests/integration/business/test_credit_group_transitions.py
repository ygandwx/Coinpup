"""Complete fictional topology proofs run explicitly; unchanged gates still forbid commits."""

from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.credit_transition_fixture import group, transition
from tests.integration.business.test_credit_projections import gated
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import credit_group_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def validate(connection, identifier):
    connection.execute(
        text("SELECT coinpup_validate_credit_group_transitions(:id)"), {"id": identifier}
    )


def rejected(connection, identifier, message):
    with pytest.raises(IntegrityError) as failure:
        validate(connection, identifier)
    assert failure.value.orig.diag.constraint_name == "ck_credit_revision_shape"
    assert failure.value.orig.diag.message_primary == message


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_complete_multi_version_membership_rejoins_with_a_new_operation_then_cancels(drafts, kind):
    s = drafts
    state = group(s, kind=kind)
    original = state["original"]
    a, b, c = state["source_ids"]
    old_b = state["current"][b]["operation_id"]
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        validate(connection, original.id)
        transition(connection, state, [a, c])
        validate(connection, original.id)
        transition(connection, state, [b, c])
        assert state["current"][b]["operation_id"] != old_b
        assert state["current"][b]["operation_version"] == 1
        validate(connection, original.id)
        transition(connection, state, None)
        validate(connection, original.id)
        gated(connection)
        transaction.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize("count,selected,replace", [(200, 200, False), (200, 100, True)])
def test_all_retained_or_disjoint_sources_cover_the_actual_document_bound(
    drafts, count, selected, replace
):
    s = drafts
    state = group(s, count=count, selected=selected)
    sources = state["source_ids"][selected:] if replace else state["source_ids"]
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        _, members = transition(connection, state, sources)
        assert len(members) == 200
        validate(connection, state["original"].id)
        gated(connection)
        transaction.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize("fault", ["omitted", "previous_position", "previous_source", "order"])
def test_complete_old_members_and_canonical_transition_order_are_required(drafts, fault):
    s = drafts
    state = group(s)
    a, _, c = state["source_ids"]
    before = snapshot(s)

    def changes(row):
        if fault == "omitted" and row["action"] == "cancel":
            return {"omit": True}
        if fault == "previous_position" and row["previous_line_no"] is not None:
            return {"previous_line_no": 3 - row["previous_line_no"]}
        if fault == "previous_source" and row["action"] == "correct":
            return {"source_operation_id": state["source_operations"][c]}
        if fault == "order":
            return {"member_no": 4 - row["member_no"]}
        return {}

    with s["engine"].connect() as connection:
        transaction = connection.begin()
        transition(connection, state, [a, c], member_changes=changes)
        rejected(connection, state["original"].id, "Credit membership is incomplete")
        transaction.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize("fault", ["skipped_version", "wrong_predecessor", "after_cancel"])
def test_group_history_is_contiguous_and_cancellation_is_terminal(drafts, fault):
    s = drafts
    state = group(s)
    sources = list(state["current"])
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        first, _ = transition(connection, state, sources)
        if fault == "wrong_predecessor":
            transition(connection, state, sources)
            changes = {"previous_revision_id": first["id"]}
        elif fault == "after_cancel":
            transition(connection, state, None)
            changes = {}
        else:
            changes = {"group_version": 4}
        transition(connection, state, sources, header_changes=changes)
        rejected(connection, state["original"].id, "Credit revision chain is inconsistent")
        transaction.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize("fault", ["reason", "reverses", "version"])
def test_reversal_identity_and_reason_are_bound_to_this_group_transition(drafts, fault):
    s = drafts
    state = group(s)
    first = next(iter(state["current"].values()))
    changes = dict(revision_reason="Fictional different reason")
    if fault == "version":
        changes = dict(operation_version=3)
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        # The wrong target is this member's replacement posting: same operation and ledger,
        # already inserted by the fixture, so the immediate FK cannot mask the target proof.
        transition(
            connection,
            state,
            [first["source_line_id"]],
            journal_changes=lambda row: (
                (dict(reverses_journal_id=row["journal_id"]) if fault == "reverses" else changes)
                if row["member_no"] == 1
                else {}
            ),
        )
        rejected(connection, state["original"].id, "Credit transition journals are invalid")
        transaction.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize("cancel", [False, True])
def test_each_owned_operation_head_must_match_its_last_recorded_event(drafts, cancel):
    s = drafts
    state = group(s)
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        transition(connection, state, None if cancel else list(state["current"]), keep_head=True)
        rejected(connection, state["original"].id, "Credit operation state is inconsistent")
        transaction.rollback()
    assert snapshot(s) == before


def test_a_previously_removed_operation_cannot_hide_outside_current_members(drafts):
    s = drafts
    state = group(s)
    retained = [state["source_ids"][1]]
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        transition(connection, state, retained, keep_head=lambda row: row["action"] == "cancel")
        transition(connection, state, retained)
        rejected(connection, state["original"].id, "Credit operation state is inconsistent")
        transaction.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize("borrow_other", [False, True])
def test_reintroduced_source_cannot_reuse_an_old_or_other_group_operation(drafts, borrow_other):
    s = drafts
    state = group(s)
    original = state["original"]
    source = original.postings[0]
    if borrow_other:
        # Another valid group on the same sources provides a real borrowed identity.
        from coinpup_api.business.credit_schemas import BusinessCreditCreate

        payload = BusinessCreditCreate(
            id=uuid4(),
            document_id=original.document_id,
            expected_version=original.source_version,
            credit_date=original.credit_date,
            reason="Fictional other group",
            lines=[
                dict(
                    source_line_id=source.source_line_id,
                    net_amount="1",
                    tax_amount="0",
                    recognition_date=source.recognition_date,
                )
            ],
        )
        source = credit(s, payload, key="fictional-borrow-source").postings[0]
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        remaining = [original.postings[1].source_line_id]
        transition(connection, state, remaining)
        transition(
            connection,
            state,
            [original.postings[0].source_line_id, *remaining],
            member_changes=lambda row: (
                dict(operation_id=source.operation_id, journal_id=source.journal_id)
                if row["action"] == "create"
                else {}
            ),
        )
        rejected(connection, original.id, "Credit operation ownership is invalid")
        transaction.rollback()
    assert snapshot(s) == before


def test_unknown_group_is_rejected_and_empty_migration_roundtrip_preserves_old_definitions(drafts):
    s = drafts
    state = group(s)
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        rejected(connection, uuid4(), "Credit revision chain is inconsistent")
        transaction.rollback()
    with (
        s["engine"].begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        functions, triggers = definitions(connection)
        migration = credit_group_migration()
        migration["downgrade"]()
        remaining, old_triggers = definitions(connection)
        assert remaining == [
            body
            for body in functions
            if not any(
                name in body.split("\n", 1)[0]
                for name in (
                    "coinpup_validate_credit_group_transitions",
                    "coinpup_business_credit_revision_response",
                    "coinpup_validate_credit_revision_receipt",
                )
            )
        ]
        assert old_triggers == triggers
        migration["upgrade"]()
        assert definitions(connection) == (functions, triggers)
        validate(connection, state["original"].id)
    assert snapshot(s) == before


def test_revision_history_refuses_transition_helper_downgrade(drafts):
    s = drafts
    state = group(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        transition(connection, state, list(state["current"]))
        with Operations.context(MigrationContext.configure(connection)):
            credit_group_migration()["downgrade"]()
    assert failure.value.orig.diag.constraint_name == "ck_credit_revision_downgrade"
    assert snapshot(s) == before
