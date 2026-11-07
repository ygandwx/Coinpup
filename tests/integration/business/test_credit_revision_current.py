"""Current command snapshots read real committed groups, never historical receipts."""

from contextlib import contextmanager
from uuid import uuid4

import pytest
from coinpup_api.business.credit_revision_current import read_current_credit
from coinpup_api.business.credits import BusinessCreditService
from coinpup_api.business.schemas import PartyUpdate, ProjectUpdate
from coinpup_api.ledger.schemas import AssetUpdate, CategoryUpdate, EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event
from sqlalchemy.orm import Session

from tests.integration.business.credit_transition_fixture import group, transition
from tests.integration.business.test_credit_current_capacity import other_credit
from tests.integration.business.test_credit_revision_lifecycle import write
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@contextmanager
def reading(s, identifier, *, queries=5):
    before = snapshot(s)
    service = BusinessCreditService(s["engine"])
    with service._transaction(s["owner"], read_only=True) as session:
        service._ledger(session, s["owner"], s["ledger"])
        statements = []

        def observe(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(s["engine"], "before_cursor_execute", observe)
        try:
            current = read_current_credit(session, s["ledger"], identifier)
        finally:
            event.remove(s["engine"], "before_cursor_execute", observe)
        assert len(statements) == queries
        assert all(statement.lstrip().upper().startswith("SELECT ") for statement in statements)
        assert all("FOR UPDATE" not in statement.upper() for statement in statements)
        yield current
    assert snapshot(s) == before


def assert_members(current, state, sources):
    assert current.credit.id == state["original"].id
    assert current.document.id == state["original"].document_id
    assert current.group_version == state["version"]
    assert [row["source_line_id"] for row in current.facts] == sources
    assert [item.identity.source_line_id for item in current.originals] == sources
    assert len(current.operations) == len(sources)
    for item, fact in zip(current.originals, current.facts, strict=True):
        expected = state["current"][item.identity.source_line_id]
        assert item.identity.operation_id == expected["operation_id"]
        assert item.identity.operation_version == expected["operation_version"]
        assert item.identity.journal_id == expected["journal_id"]
        assert item.source_operation_id == state["source_operations"][fact["source_line_id"]]
        assert item.journal.description == fact["credit_reason"]
        assert item.journal.recognition_date == fact["recognition_date"]
        assert [row.line_no for row in item.lines] == [1, 2]
        assert {row.document_line_id for row in item.lines} == {fact["source_line_id"]}
        assert {row.document_id for row in item.lines} == {current.document.id}
        assert current.operations[item.identity.operation_id].status == "active"


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_complete_current_group_survives_multiple_commits_and_terminal_cancel(drafts, kind):
    s = drafts
    state = group(s, kind=kind)
    a, b, c = state["source_ids"]
    old_b = state["current"][b]["operation_id"]
    with reading(s, state["original"].id) as current:
        assert_members(current, state, [a, b])
        assert current.revision is None
        assert not current.cancelled
    # Sharing a source with an independent credit must not add its member to this group.
    other, _ = other_credit(s, state, a)
    for sources in ([a, c], [b, c], [a, b, c]):
        with s["engine"].begin() as connection:
            header, _, _ = write(connection, state, sources)
        with reading(s, state["original"].id) as current:
            assert_members(current, state, sources)
            assert current.revision.id == header["id"]
            assert current.revision.version == 1
            assert current.group_version != current.revision.version
            assert all(row["credit_id"] != other.id for row in current.facts)
            if b in sources:
                assert current.originals[sources.index(b)].identity.operation_id != old_b
            if sources == [b, c]:
                assert [item.identity.operation_version for item in current.originals] == [1, 2]
    with s["engine"].begin() as connection:
        write(connection, state, None)
    with reading(s, state["original"].id, queries=3) as current:
        assert current.cancelled
        assert_members(current, state, [])
        assert current.facts == current.originals == ()
        assert current.operations == {}


@pytest.mark.parametrize("count", [1, 200])
def test_member_count_does_not_change_batch_query_count(drafts, count):
    s = drafts
    state = group(s, count=count, selected=count)
    with reading(s, state["original"].id) as current:
        assert_members(current, state, state["source_ids"])
        assert sum(len(item.lines) for item in current.originals) == 2 * count
    sources = list(reversed(state["source_ids"]))
    with s["engine"].begin() as connection:
        write(connection, state, sources)
    with reading(s, state["original"].id) as current:
        assert_members(current, state, sources)
        assert sum(len(item.lines) for item in current.originals) == 2 * count


@pytest.mark.parametrize("cancelled", [False, True])
def test_disabled_archived_references_do_not_hide_current_facts(drafts, cancelled):
    s = drafts
    state = group(s)
    sources = state["source_ids"][1:]
    with s["engine"].begin() as connection:
        write(connection, state, sources)
    if cancelled:
        with s["engine"].begin() as connection:
            write(connection, state, None)
    s["master"].update_party(
        s["owner"], s["ledger"], s["party"].id, PartyUpdate(expected_version=1, archived=True)
    )
    s["master"].update_project(
        s["owner"], s["ledger"], s["project"].id, ProjectUpdate(expected_version=1, archived=True)
    )
    s["structure"].update_category(
        s["owner"], s["ledger"], s["income"].id, CategoryUpdate(expected_version=1, archived=True)
    )
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    with reading(s, state["original"].id, queries=3 if cancelled else 5) as current:
        assert_members(current, state, [] if cancelled else sources)
        assert current.cancelled is cancelled


def test_known_credit_in_a_different_real_ledger_is_not_found(drafts):
    s = drafts
    state = group(s)
    foreign = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional unrelated ledger",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    before = snapshot(s)
    with Session(s["engine"]) as session:
        for ledger, identifier in (
            (foreign.ledger.id, state["original"].id),
            (s["ledger"], uuid4()),
        ):
            with pytest.raises(LedgerError) as failure:
                read_current_credit(session, ledger, identifier)
            assert (failure.value.code, failure.value.status) == ("not_found", 404)
    assert snapshot(s) == before


def test_pending_revision_with_unadvanced_operation_is_rejected_by_reader_before_commit(drafts):
    s = drafts
    state = group(s)
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        transition(connection, state, state["source_ids"][:2], keep_head=True)
        with Session(bind=connection) as session:
            with pytest.raises(LedgerError) as failure:
                read_current_credit(session, s["ledger"], state["original"].id)
                pytest.fail("The reader accepted current facts with obsolete operation heads")
            assert (failure.value.code, failure.value.status) == ("credit_revision_integrity", 503)
        assert transaction.is_active
        transaction.rollback()
    assert snapshot(s) == before
