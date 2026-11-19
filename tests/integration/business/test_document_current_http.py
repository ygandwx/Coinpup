"""Real current reads preserve complete versions, read-only scope and immutable original 201s."""

from concurrent.futures import ThreadPoolExecutor
from threading import get_ident
from uuid import uuid4

import pytest
from coinpup_api.business.document_current import BusinessDocumentCurrentReads
from coinpup_api.business.document_revision_schemas import ConfirmedDocumentCancellation
from coinpup_api.ledger.schemas import AssetUpdate, EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event

from tests.integration.business.test_document_revision_commands import correction, send
from tests.integration.business.test_document_revision_http import frozen, post
from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_document_revision_storage import reviewed
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def path(s, doc):
    return f"/api/v1/ledgers/{s['ledger']}/business-documents/{doc.id}/current"


@pytest.mark.parametrize("kind", ["invoice", "bill"])
@pytest.mark.parametrize("count", [1, 200])
def test_real_current_read_tracks_complete_commits_and_keeps_original_201(
    drafts, authenticated_client, kind, count
):
    s = drafts
    client, _, _ = authenticated_client
    doc, postings = confirmed(s, kind, count)
    original_path = path(s, doc).replace("/current", "/confirmation")
    original = client.get(original_path).json()
    before = snapshot(s)
    first = client.get(path(s, doc))
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    first = first.json()
    assert first["document"] == original["document"] and first["revision_id"] is None
    assert first["version"] == doc.version and first["status"] == "active"
    assert [p["source_line_id"] for p in first["postings"]] == [str(r.id) for r in doc.lines]
    assert [p["operation_id"] for p in first["postings"]] == [str(p.operation_id) for p in postings]
    assert all(p["operation_version"] == 1 for p in first["postings"])
    assert snapshot(s) == before
    intent = frozen(s, doc, correction(doc, notes="Fictional revised 中文"), "fictional-current")
    fixed = post(client, intent).json()
    before = snapshot(s)
    current = client.get(path(s, doc)).json()
    assert current["revision_id"] == fixed["revision_id"]
    assert (
        current["version"] == doc.version + 1
        and current["document"]["notes"] == "Fictional revised 中文"
    )
    assert len(current["postings"]) == count
    for p, r in zip(current["postings"], fixed["replacement"]["lines"], strict=True):
        assert p == r["posting"] | {"source_line_id": r["id"]}
    assert client.get(original_path).json() == original and snapshot(s) == before
    ending = ConfirmedDocumentCancellation(
        expected_version=fixed["version"], reason="Fictional end"
    )
    ended = send(s, doc, ending, "fictional-current-end")
    change_period(s, cutoff="2026-11-06")
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    terminal = client.get(path(s, doc)).json()
    assert terminal == first | dict(
        version=ended.version,
        revision_id=str(ended.revision_id),
        status="cancelled",
        document=None,
        postings=[],
    )
    assert client.get(original_path).json() == original
    assert post(client, intent).json() == fixed and snapshot(s) == before


@pytest.mark.parametrize("asset,unit", [("BTC", "0.00000001"), ("ETH", "0.000000000000000001")])
def test_zero_positive_added_and_removed_lines_read_their_actual_current_units(
    drafts, authenticated_client, asset, unit
):
    s = drafts
    client, _, _ = authenticated_client
    doc, _ = confirmed(s, "invoice", 4)
    payload = correction(doc)
    rows = [
        r.model_copy(update={"unit_price": "0" if i == 1 else unit})
        for i, r in enumerate(payload.replacement.lines)
    ]
    rows.append(rows[0].model_copy(update={"id": uuid4()}))
    rows = rows[1:]
    payload = payload.model_copy(update={"replacement": reviewed(doc, rows, asset_id=asset)})
    fixed = send(s, doc, payload, "fictional-current-units")
    before = snapshot(s)
    current = client.get(path(s, doc)).json()
    assert current["document"]["asset_id"] == asset
    assert [r["id"] for r in current["document"]["lines"]] == [str(r.id) for r in rows]
    assert [p["source_line_id"] for p in current["postings"]] == [str(r.id) for r in rows[1:]]
    assert all(p["operation_version"] == 1 for p in current["postings"])
    assert [r["total_amount"] for r in current["document"]["lines"]] == [
        r.total_amount for r in fixed.replacement.lines
    ]
    assert snapshot(s) == before


def test_current_read_rejects_draft_other_owned_ledger_unknown_and_unauthenticated(
    drafts, authenticated_client
):
    s = drafts
    client, _, _ = authenticated_client
    doc, _ = confirmed(s, "bill", 1)
    draft = s["service"].create_draft(s["owner"], s["ledger"], s["payload"]())
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional other current scope",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    before = snapshot(s)
    for url in [
        path(s, draft),
        path(s, doc).replace(str(doc.id), str(uuid4())),
        path(s, doc).replace(str(s["ledger"]), str(other.ledger.id)),
    ]:
        denied = client.get(url)
        assert denied.status_code == 404 and denied.json()["detail"]["code"] == "not_found"
    with pytest.raises(LedgerError) as wrong_owner:
        BusinessDocumentCurrentReads(s["engine"]).current(uuid4(), s["ledger"], doc.id)
    assert wrong_owner.value.code == "not_found"
    assert snapshot(s) == before
    client.cookies.clear()
    assert client.get(path(s, doc)).status_code == 401


def test_active_current_facts_remain_readable_after_archive_and_disabled_asset(
    drafts, authenticated_client
):
    s = drafts
    client, _, _ = authenticated_client
    doc, _ = confirmed(s, "bill", 4)
    send(s, doc, correction(doc), "fictional-read-before-archive")
    active = client.get(path(s, doc)).json()
    assert active["status"] == "active" and len(active["document"]["lines"]) == 4
    assert len(active["postings"]) == 2
    change_period(s, cutoff="2026-11-06")
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    assert client.get(path(s, doc)).json() == active and snapshot(s) == before


@pytest.mark.parametrize("count", [1, 200])
def test_current_query_count_is_bounded_and_never_takes_a_write_lock(drafts, count):
    s = drafts
    doc, _ = confirmed(s, "bill", count)
    fixed = send(s, doc, correction(doc), "fictional-current-bounded")
    statements = []

    def capture(conn, cursor, statement, params, context, many):
        statements.append(statement.lower())

    before = snapshot(s)
    event.listen(s["engine"], "before_cursor_execute", capture)
    try:
        current = BusinessDocumentCurrentReads(s["engine"]).current(s["owner"], s["ledger"], doc.id)
    finally:
        event.remove(s["engine"], "before_cursor_execute", capture)
    assert current.version == fixed.version and len(current.postings) == count
    assert len([q for q in statements if q.lstrip().startswith("select")]) <= 12
    assert not any(
        word in q
        for q in statements
        for word in ("pg_advisory", "for update", "insert into", "update ", "delete from")
    )
    assert snapshot(s) == before


def test_concurrent_commit_cannot_mix_current_header_sources_and_accruals(drafts):
    s = drafts
    doc, _ = confirmed(s, "invoice", 4)
    old = send(
        s, doc, correction(doc, notes="Fictional previous snapshot"), "fictional-current-before"
    )
    payload = correction(doc, notes="Fictional committed concurrently").model_copy(
        update={"expected_version": old.version}
    )
    reader = BusinessDocumentCurrentReads(s["engine"])
    before = reader.current(s["owner"], s["ledger"], doc.id)
    thread = get_ident()
    done = []

    def after_header(conn, cursor, statement, parameters, context, many):
        if (
            get_ident() == thread
            and not done
            and statement.startswith("SELECT business_document_revisions.")
        ):
            done.append(True)
            with ThreadPoolExecutor(max_workers=1) as pool:
                done.append(
                    pool.submit(send, s, doc, payload, "fictional-current-concurrent").result(
                        timeout=30
                    )
                )

    event.listen(s["engine"], "after_cursor_execute", after_header)
    try:
        during = reader.current(s["owner"], s["ledger"], doc.id)
    finally:
        event.remove(s["engine"], "after_cursor_execute", after_header)
    assert len(done) == 2 and during == before
    after = reader.current(s["owner"], s["ledger"], doc.id)
    assert after.version == old.version + 1 and after.document.notes == payload.replacement.notes
    assert after.postings != before.postings
