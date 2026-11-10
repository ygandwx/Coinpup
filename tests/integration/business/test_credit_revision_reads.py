"""Actual committed revisions, constant authorized reads and an unmixed MVCC snapshot."""

from uuid import uuid4

import pytest
from coinpup_api.business.credit_revision_reads import BusinessCreditRevisionReads
from coinpup_api.business.schemas import PartyUpdate
from coinpup_api.ledger.schemas import AssetUpdate, CategoryUpdate, EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event

from tests.integration.business.credit_transition_fixture import group
from tests.integration.business.test_credit_current_capacity import other_credit
from tests.integration.business.test_credit_revision_service import command
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def read_queries(s, call, expected):
    before, statements = snapshot(s), []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.upper().lstrip())

    event.listen(s["engine"], "before_cursor_execute", capture)
    try:
        result = call()
    finally:
        event.remove(s["engine"], "before_cursor_execute", capture)
    assert sum(statement.startswith("SELECT") for statement in statements) == expected
    assert all(statement.startswith(("SELECT", "SET TRANSACTION")) for statement in statements)
    assert all(
        "FOR UPDATE" not in statement and "PG_ADVISORY" not in statement for statement in statements
    )
    assert snapshot(s) == before
    return result


def get(s, client, identifier):
    response = read_queries(
        s,
        lambda: client.get(f"/api/v1/ledgers/{s['ledger']}/business-credits/{identifier}/current"),
        6,
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_current_http_tracks_complete_commits_and_keeps_the_original_detail(
    drafts, authenticated_client, kind
):
    s = drafts
    client, _, _ = authenticated_client
    state = group(s, kind=kind)
    original = state["original"]
    a, b, c = state["source_ids"]
    old_b = original.postings[1].operation_id
    path = f"/api/v1/ledgers/{s['ledger']}/business-credits/{original.id}"
    detail = client.get(path).json()
    first = get(s, client, original.id)
    assert first["version"] == 1 and first["status"] == "active"
    assert first["revision_id"] is first["revision_reason"] is None
    assert first["document_kind"] == kind and first["asset_id"] == "USD"
    assert first["credit_date"] == str(original.credit_date)
    assert first["credit_reason"] == state["request"].reason
    assert [row["source_line_id"] for row in first["members"]] == [str(a), str(b)]
    # This independent group's current source must not leak into the selected group.
    other_credit(s, state, a)
    for version, sources in enumerate(([a, c], [b, c], [a, b, c], None), 1):
        saved = command(s, state, sources, version=version)[-1]
        current = get(s, client, original.id)
        assert current["version"] == saved.version and current["revision_id"] == str(
            saved.revision_id
        )
        assert current["revision_reason"] == saved.reason
        assert current["credit_id"] == str(original.id) and current["document_id"] == str(
            original.document_id
        )
        expected = [item for item in saved.members if item.action != "cancel"]
        assert [row["source_line_id"] for row in current["members"]] == [
            str(source) for source in sources or []
        ]
        for row, item in zip(current["members"], expected, strict=True):
            assert (row["operation_id"], row["operation_version"], row["journal_id"]) == (
                str(item.operation_id),
                item.operation_version,
                str(item.journal_id),
            )
            assert row["source_operation_id"] == str(item.source_operation_id)
            assert (row["net_amount"], row["tax_amount"], row["total_amount"]) == (
                "17.23",
                "2.77",
                "20.00",
            )
            if item.source_line_id == b:
                assert row["operation_id"] != str(old_b)
        if sources is None:
            assert current["status"] == "cancelled" and current["members"] == []
            assert current["credit_date"] is current["credit_reason"] is None
        else:
            assert current["status"] == "active" and current["credit_date"] == str(
                saved.credit_date
            )
            assert current["credit_reason"] == saved.replacement_reason
        before = snapshot(s)
        assert client.get(path).json() == detail
        assert credit(s, state["request"]) == original
        assert snapshot(s) == before


@pytest.mark.parametrize("count", [1, 200])
def test_five_service_queries_for_first_reordered_and_cancelled_complete_groups(drafts, count):
    s = drafts
    state = group(s, count=count, selected=count)
    reader = BusinessCreditRevisionReads(s["engine"])

    def read():
        return reader.current(s["owner"], s["ledger"], state["original"].id)

    original = read_queries(s, read, 5)
    assert len(original.members) == count and original.version == 1
    sources = list(reversed(state["source_ids"]))
    command(s, state, sources)
    corrected = read_queries(s, read, 5)
    assert [row.source_line_id for row in corrected.members] == sources
    assert [row.line_no for row in corrected.members] == list(range(1, count + 1))
    command(s, state, None, version=2)
    cancelled = read_queries(s, read, 5)
    assert cancelled.version == 3 and cancelled.members == []


@pytest.mark.parametrize("cancelled", [False, True])
def test_current_http_preserves_archived_disabled_and_closed_history(
    drafts, authenticated_client, cancelled
):
    s = drafts
    client, _, _ = authenticated_client
    state = group(s)
    command(s, state, state["source_ids"][1:])
    if cancelled:
        command(s, state, None, version=2)
    before = get(s, client, state["original"].id)
    change_period(s, cutoff="2026-12-31")
    s["master"].update_party(
        s["owner"], s["ledger"], s["party"].id, PartyUpdate(expected_version=1, archived=True)
    )
    s["structure"].update_category(
        s["owner"], s["ledger"], s["income"].id, CategoryUpdate(expected_version=1, archived=True)
    )
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    assert get(s, client, state["original"].id) == before


def test_real_scope_and_unknown_owner_never_disclose_current_group(drafts, authenticated_client):
    s = drafts
    client, _, _ = authenticated_client
    state = group(s)
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional unrelated current group",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    before = snapshot(s)
    for ledger, identifier in ((other.ledger.id, state["original"].id), (s["ledger"], uuid4())):
        response = client.get(f"/api/v1/ledgers/{ledger}/business-credits/{identifier}/current")
        assert response.status_code == 404 and response.json()["detail"]["code"] == "not_found"
        assert response.headers["cache-control"] == "no-store"
    with pytest.raises(LedgerError) as failure:
        BusinessCreditRevisionReads(s["engine"]).current(uuid4(), s["ledger"], state["original"].id)
    assert (failure.value.code, failure.value.status) == ("not_found", 404)
    assert snapshot(s) == before


def test_current_metadata_and_members_keep_one_snapshot_during_another_commit(drafts):
    s = drafts
    state = group(s)
    reader = BusinessCreditRevisionReads(s["engine"])

    def read():
        return reader.current(s["owner"], s["ledger"], state["original"].id)

    first = read()
    commits = []

    def commit_after_metadata(connection, _cursor, statement, _parameters, _context, _many):
        if "latest_credit_revision" not in statement or commits:
            return
        commits.append(connection)
        saved = command(s, state, state["source_ids"][1:])[-1]
        commits.append((saved, snapshot(s)))

    event.listen(s["engine"], "after_cursor_execute", commit_after_metadata)
    try:
        during = read()
    finally:
        event.remove(s["engine"], "after_cursor_execute", commit_after_metadata)
    assert len(commits) == 2 and during == first
    saved, after_write = commits[1]
    assert snapshot(s) == after_write
    after = read()
    assert after.version == saved.version and after.revision_id == saved.revision_id
    assert [row.source_line_id for row in after.members] == state["source_ids"][1:]
    assert snapshot(s) == after_write
