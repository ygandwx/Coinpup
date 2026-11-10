"""Committed version directories and fixed receipts remain private, complete and read-only."""

from copy import deepcopy
from uuid import uuid4

import pytest
from coinpup_api.business.credit_revision_history_reads import BusinessCreditHistory, _revision
from coinpup_api.ledger.schemas import AssetUpdate, EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event
from sqlalchemy.orm import Session

from tests.integration.business.credit_transition_fixture import group
from tests.integration.business.test_credit_revision_receipts import (
    candidate,
    content,
    save_receipt,
)
from tests.integration.business.test_credit_revision_service import command
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def counted(s, call, expected):
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(s["engine"], "before_cursor_execute", capture)
    try:
        result = call()
    finally:
        event.remove(s["engine"], "before_cursor_execute", capture)
    assert sum(sql.lstrip().upper().startswith("SELECT") for sql in statements) == expected
    assert any("REPEATABLE READ, READ ONLY" in sql for sql in statements)
    assert not any(
        "FOR UPDATE" in sql or sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for sql in statements
    )
    return result


@pytest.mark.parametrize("kind,count", [("invoice", 1), ("bill", 200)])
def test_real_directory_and_complete_details_have_constant_queries_after_cancel_and_closure(
    drafts, authenticated_client, kind, count
):
    s = drafts
    client, _, _ = authenticated_client
    state = group(s, kind=kind, count=count, selected=count)
    original = state["original"]
    base = f"/api/v1/ledgers/{s['ledger']}/business-credits/{original.id}"
    reads = BusinessCreditHistory(s["engine"])
    original_detail = client.get(base).json()
    before = snapshot(s)
    first = counted(s, lambda: reads.list_history(s["owner"], s["ledger"], original.id), 5)
    assert len(first) == 1 and (first[0].version, first[0].action, first[0].revision_id) == (
        1,
        "create",
        None,
    )
    assert first[0].reason == state["request"].reason
    assert credit(s, state["request"]) == original and snapshot(s) == before
    corrected = command(s, state, list(reversed(state["source_ids"])))[-1]
    cancelled = command(s, state, None, version=2)[-1]
    assert len(corrected.members) == len(cancelled.members) == count
    expected_directory = None
    for closed in (False, True):
        if closed:
            change_period(s, cutoff="2026-11-06")
            s["structure"].update_asset(
                s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False)
            )
            s["structure"].update_entity(
                s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
            )
        before = snapshot(s)
        directory = counted(s, lambda: client.get(base + "/history"), 6)
        assert directory.status_code == 200 and directory.headers["cache-control"] == "no-store"
        entries = directory.json()
        assert [(row["version"], row["action"]) for row in entries] == [
            (1, "create"),
            (2, "correct"),
            (3, "cancel"),
        ]
        assert all(
            set(row) == {"version", "revision_id", "action", "reason", "created_at"}
            for row in entries
        )
        assert entries[0] == first[0].model_dump(mode="json")
        assert [row["revision_id"] for row in entries[1:]] == [
            str(corrected.revision_id),
            str(cancelled.revision_id),
        ]
        if expected_directory is not None:
            assert entries == expected_directory
        expected_directory = entries
        assert client.get(base + "/history?limit=1&offset=1").json() == entries[1:2]
        assert client.get(base + "/history?offset=3").json() == []
        for saved in (corrected, cancelled):
            detail = counted(
                s,
                lambda saved=saved: reads.get_revision(
                    s["owner"], s["ledger"], original.id, saved.revision_id
                ),
                5,
            )
            assert detail.model_dump(mode="json") == saved.model_dump(mode="json")
            response = counted(
                s, lambda saved=saved: client.get(base + f"/revisions/{saved.revision_id}"), 6
            )
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            assert response.json() == saved.model_dump(mode="json")
            assert len(response.json()["members"]) == count
        assert client.get(base).json() == original_detail
        assert credit(s, state["request"]) == original
        assert snapshot(s) == before


def test_history_scope_auth_and_pagination_fail_before_fixed_sql(drafts, authenticated_client):
    s = drafts
    client, _, _ = authenticated_client
    state = group(s)
    original = state["original"]
    saved = command(s, state, state["source_ids"][1:])[-1]
    other = credit(
        s, state["request"].model_copy(update={"id": uuid4()}), key="fictional-other-history"
    )
    foreign = (
        s["structure"]
        .create_entity(
            s["owner"],
            EntityCreate(
                kind="personal",
                name="Fictional history scope",
                base_asset_id="USD",
                template_key="personal_default",
            ),
        )
        .ledger.id
    )
    base = f"/api/v1/ledgers/{s['ledger']}/business-credits/{original.id}"
    paths = [
        base.replace(str(s["ledger"]), str(foreign)) + "/history",
        base.replace(str(original.id), str(uuid4())) + "/history",
        base.replace(str(s["ledger"]), str(foreign)) + f"/revisions/{saved.revision_id}",
        base.replace(str(original.id), str(other.id)) + f"/revisions/{saved.revision_id}",
        base + f"/revisions/{uuid4()}",
    ]
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    before = snapshot(s)
    event.listen(s["engine"], "before_cursor_execute", capture)
    try:
        for path in paths:
            denied = client.get(path)
            assert denied.status_code == 404 and denied.json()["detail"]["code"] == "not_found"
            assert denied.headers["cache-control"] == "no-store"
        for query in ("limit=0", "limit=201", "offset=-1", "offset=100001"):
            assert client.get(base + "/history?" + query).status_code == 422
        reads = BusinessCreditHistory(s["engine"])
        for call in (
            lambda: reads.list_history(uuid4(), s["ledger"], original.id),
            lambda: reads.get_revision(uuid4(), s["ledger"], original.id, saved.revision_id),
        ):
            with pytest.raises(LedgerError) as failure:
                call()
            assert failure.value.status == 404
        client.cookies.clear()
        for suffix in ("/history", f"/revisions/{saved.revision_id}"):
            denied = client.get(base + suffix)
            assert denied.status_code == 401 and denied.headers["cache-control"] == "no-store"
    finally:
        event.remove(s["engine"], "before_cursor_execute", capture)
    assert not any("coinpup_business_credit_revision_response" in sql for sql in statements)
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "fault", ["missing", "hash", "status", "anchor", "uuid", "integer", "string", "fact"]
)
def test_pending_bad_receipts_are_rejected_not_rebuilt_or_normalized(drafts, fault):
    s = drafts
    state = group(s)
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        header, _, expected = candidate(
            connection,
            state,
            state["source_ids"][1:],
            **(
                {
                    "member_changes": lambda row: (
                        {"previous_journal_id": uuid4()} if row["action"] == "correct" else {}
                    )
                }
                if fault == "fact"
                else {}
            ),
        )
        value = deepcopy(expected)
        changes = {}
        if fault == "hash":
            changes["hash_version"] = 1
        elif fault == "status":
            changes["response_status"] = 201
        elif fault == "anchor":
            changes["operation_id"] = state["original"].postings[0].source_operation_id
        elif fault == "uuid":
            value["revision_id"] = value["revision_id"].replace("-", "").upper()
        elif fault == "integer":
            value["version"] = 2.0
        elif fault == "string":
            value["members"][0]["net_amount"] = "17.23"
        if fault != "missing":
            save_receipt(connection, header, value, **changes)
        pending = content(connection)
        with Session(bind=connection) as session:
            with pytest.raises(LedgerError) as failure, session.begin_nested():
                _revision(session, s["ledger"], state["original"].id, header["id"])
                pytest.fail("The selected stored receipt was silently reconstructed")
            assert (failure.value.code, failure.value.status) == ("credit_revision_integrity", 503)
        assert transaction.is_active and content(connection) == pending
        transaction.rollback()
    assert snapshot(s) == before


def test_history_uses_one_repeatable_read_snapshot_across_concurrent_commit(drafts):
    s = drafts
    state = group(s)
    reads = BusinessCreditHistory(s["engine"])
    committed = []

    def after_scope(_connection, _cursor, statement, _parameters, _context, _many):
        if (
            not committed
            and "SELECT business_credits.id" in statement
            and "FROM business_credits" in statement
        ):
            committed.append(None)  # The nested real writer must not re-enter this callback.
            committed[0] = command(s, state, state["source_ids"][1:])[-1]

    event.listen(s["engine"], "after_cursor_execute", after_scope)
    try:
        old = reads.list_history(s["owner"], s["ledger"], state["original"].id)
    finally:
        event.remove(s["engine"], "after_cursor_execute", after_scope)
    assert committed[0].version == 2
    assert [entry.version for entry in old] == [1]
    before = snapshot(s)
    current = reads.list_history(s["owner"], s["ledger"], state["original"].id)
    assert [entry.version for entry in current] == [1, 2]
    assert current[1].revision_id == committed[0].revision_id
    assert snapshot(s) == before
