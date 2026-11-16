"""Read original, effective and terminal return groups without changing any financial fact."""

from uuid import uuid4

import pytest
from coinpup_api.business.allocation_return_current_read import BusinessAllocationReturnCurrentRead
from coinpup_api.ledger.schemas import AssetUpdate, EntityCreate
from sqlalchemy import event

from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_return_revision_service import cancel, command, run
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def tracked(s, identifier):
    statements = []

    def capture(conn, cursor, sql, *args):
        statements.append(sql.lstrip().upper())

    event.listen(s["engine"], "before_cursor_execute", capture)
    try:
        result = BusinessAllocationReturnCurrentRead(s["engine"]).current(
            s["owner"], s["ledger"], identifier
        )
    finally:
        event.remove(s["engine"], "before_cursor_execute", capture)
    assert sum(q.startswith("SELECT") for q in statements) == (
        8 if result.status == "active" else 5
    )
    assert all(
        q.startswith(("SELECT", "SET TRANSACTION"))
        and "FOR UPDATE" not in q
        and "PG_ADVISORY" not in q
        for q in statements
    )
    return result.model_dump(mode="json")


@pytest.mark.parametrize("count,direction,asset", [(1, "payout", "USD"), (200, "receipt", "EUR")])
def test_current_group_scales_with_fixed_selects_and_stays_available_after_retirement(
    drafts, authenticated_client, count, direction, asset
):
    s = drafts
    client, _, _ = authenticated_client
    _, _, _, body = setup_return(s, direction=direction, asset=asset, count=count)
    original = submit_return(s, body)
    prefix = f"/api/v1/ledgers/{s['ledger']}/business-allocation-returns/{original.id}"
    detail = client.get(prefix).json()

    def current():
        before = snapshot(s)
        expected = tracked(s, original.id)
        response = client.get(prefix + "/current")
        assert (response.status_code, response.json()) == (200, expected)
        assert response.headers["cache-control"] == "no-store" and snapshot(s) == before
        return expected

    first = current()
    assert first["version"] == 1 and first["revision_id"] is first["revision_reason"] is None
    assert first["payment_direction"] == direction and first["payment_asset_id"] == "USD"
    assert first["fact"] == dict(return_date=str(body.return_date), reason=body.reason)
    assert [m["journal_id"] for m in first["members"]] == [
        str(m.journal_id) for m in original.postings
    ]
    edited = run(s, original.id, command(body))
    second = current()
    assert (second["version"], second["revision_id"], second["revision_reason"]) == (
        2,
        str(edited.revision_id),
        edited.reason,
    )
    assert [m["operation_id"] for m in second["members"]] == [
        str(m.operation_id) for m in edited.members
    ]
    assert [m["line_no"] for m in second["members"]] == list(range(1, count + 1))
    assert all(
        m["source_return_no"] == 1
        and m["operation_version"] == 2
        and m["payment_amount"] == ("22.00" if asset == "EUR" else "20.00")
        and m["document_asset_id"] == asset
        and m["document_amount"] == "20.00"
        for m in second["members"]
    )
    s["structure"].update_asset(s["owner"], asset, AssetUpdate(expected_version=1, enabled=False))
    assert current() == second
    ended = run(s, original.id, cancel(2), "fictional-stop-read")
    assert current() == second | dict(
        version=3,
        status="cancelled",
        revision_id=str(ended.revision_id),
        revision_reason=ended.reason,
        fact=None,
        members=[],
    )
    assert client.get(prefix).json() == detail and submit_return(s, body) == original


def test_current_group_uses_one_snapshot_during_a_commit(drafts):
    s = drafts
    _, _, _, body = setup_return(s, count=2)
    original = submit_return(s, body)
    original_state = tracked(s, original.id)
    changed = []

    def after_header(conn, cursor, sql, *args):
        if not changed and "LEFT OUTER JOIN LATERAL" in sql:
            changed.append(True)
            run(s, original.id, command(body))

    event.listen(s["engine"], "after_cursor_execute", after_header)
    try:
        current = BusinessAllocationReturnCurrentRead(s["engine"]).current(
            s["owner"], s["ledger"], original.id
        )
    finally:
        event.remove(s["engine"], "after_cursor_execute", after_header)
    assert changed and current.model_dump(mode="json") == original_state
    assert tracked(s, original.id)["version"] == 2


def test_foreign_absent_and_signed_out_reads_are_private(drafts, authenticated_client):
    s = drafts
    client, _, _ = authenticated_client
    _, _, _, body = setup_return(s)
    created = submit_return(s, body)
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional unrelated return",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    before = snapshot(s)
    for ledger, group in [(other.ledger.id, created.id), (s["ledger"], uuid4())]:
        response = client.get(
            f"/api/v1/ledgers/{ledger}/business-allocation-returns/{group}/current"
        )
        assert response.status_code == 404 and response.json()["detail"]["code"] == "not_found"
        assert response.headers["cache-control"] == "no-store"
    client.cookies.clear()
    denied = client.get(
        f"/api/v1/ledgers/{s['ledger']}/business-allocation-returns/{created.id}/current"
    )
    assert denied.status_code == 401 and snapshot(s) == before
