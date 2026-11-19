"""Actual document histories retain original confirmation and each complete fixed revision."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import get_ident
from types import SimpleNamespace
from uuid import uuid4

import pytest
from coinpup_api.business.document_history import BusinessDocumentHistory
from coinpup_api.business.document_revision_schemas import ConfirmedDocumentCancellation
from coinpup_api.ledger.models import CommandReceipt
from coinpup_api.ledger.schemas import AssetUpdate, EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event
from sqlalchemy.orm import Session

from tests.integration.business.test_credit_revision_history import counted
from tests.integration.business.test_document_revision_commands import correction, send
from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def base(s, doc):
    return f"/api/v1/ledgers/{s['ledger']}/business-documents/{doc.id}"


@pytest.mark.parametrize("kind,count", [("invoice", 1), ("bill", 200)])
def test_fixed_complete_history_and_pagination_survive_cancel_archive_and_disabled_asset(
    drafts, authenticated_client, kind, count
):
    s = drafts
    client, _, _ = authenticated_client
    doc, _ = confirmed(s, kind, count)
    reads = BusinessDocumentHistory(s["engine"])
    original = client.get(base(s, doc) + "/confirmation").json()
    first = counted(s, lambda: reads.list_history(s["owner"], s["ledger"], doc.id), 5)
    assert len(first) == 1 and (
        first[0].version,
        first[0].action,
        first[0].revision_id,
        first[0].reason,
    ) == (doc.version, "confirm", None, None)
    corrected = send(s, doc, correction(doc), "fictional-document-history")
    cancelled = send(
        s,
        doc,
        ConfirmedDocumentCancellation(
            expected_version=corrected.version, reason="Fictional history end"
        ),
        "fictional-document-history-end",
    )
    old_directory = None
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
        response = counted(s, lambda: client.get(base(s, doc) + "/history"), 6)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        directory = response.json()
        assert [(r["version"], r["action"]) for r in directory] == [
            (doc.version, "confirm"),
            (doc.version + 1, "correct"),
            (doc.version + 2, "cancel"),
        ]
        assert [(r["previous_line_count"], r["replacement_line_count"]) for r in directory] == [
            (None, None),
            (count, len(corrected.replacement.lines)),
            (len(corrected.replacement.lines), 0),
        ]
        if old_directory:
            assert directory == old_directory
        old_directory = directory
        assert client.get(base(s, doc) + "/history?limit=1&offset=1").json() == directory[1:2]
        assert client.get(base(s, doc) + "/history?offset=3").json() == []
        for saved in (corrected, cancelled):
            detail = counted(
                s,
                lambda saved=saved: reads.get_revision(
                    s["owner"], s["ledger"], doc.id, saved.revision_id
                ),
                7,
            )
            assert detail == saved
            response = counted(
                s,
                lambda saved=saved: client.get(base(s, doc) + f"/revisions/{saved.revision_id}"),
                8,
            )
            assert response.status_code == 200 and response.json() == saved.model_dump(mode="json")
            assert (
                response.headers["cache-control"] == "no-store"
                and len(response.json()["previous"]["lines"]) == count
            )
        assert (
            client.get(base(s, doc) + "/confirmation").json() == original and snapshot(s) == before
        )


@pytest.mark.parametrize("asset,unit", [("BTC", "0.00000001"), ("ETH", "0.000000000000000001")])
def test_each_fixed_detail_uses_its_own_previous_and_replacement_asset(
    drafts, authenticated_client, asset, unit
):
    s = drafts
    client, _, _ = authenticated_client
    doc, _ = confirmed(s, "invoice", 1)
    payload = correction(doc, asset_id=asset)
    payload = payload.model_copy(
        update={
            "replacement": payload.replacement.model_copy(
                update={
                    "lines": [payload.replacement.lines[0].model_copy(update={"unit_price": unit})]
                }
            )
        }
    )
    first = send(s, doc, payload, "fictional-history-new-units")
    second = send(
        s,
        doc,
        correction(doc).model_copy(update={"expected_version": first.version}),
        "fictional-history-back-to-usd",
    )
    before = snapshot(s)
    for saved in (first, second):
        response = client.get(base(s, doc) + f"/revisions/{saved.revision_id}")
        assert response.status_code == 200 and response.json() == saved.model_dump(mode="json")
    assert first.previous.asset_id == "USD" and first.replacement.asset_id == asset
    assert second.previous.asset_id == asset and second.replacement.asset_id == "USD"
    assert snapshot(s) == before


def test_unauthorized_or_wrong_scope_never_executes_fixed_evidence_function(
    drafts, authenticated_client
):
    s = drafts
    client, _, _ = authenticated_client
    doc, _ = confirmed(s, "bill", 1)
    saved = send(s, doc, correction(doc), "fictional-history-scope")
    other, _ = confirmed(s, "bill", 1, key="fictional-other-history-document")
    foreign = (
        s["structure"]
        .create_entity(
            s["owner"],
            EntityCreate(
                kind="personal",
                name="Fictional foreign history",
                base_asset_id="USD",
                template_key="personal_default",
            ),
        )
        .ledger.id
    )
    path = base(s, doc)
    paths = [
        path.replace(str(s["ledger"]), str(foreign)) + "/history",
        path.replace(str(doc.id), str(uuid4())) + "/history",
        path.replace(str(s["ledger"]), str(foreign)) + f"/revisions/{saved.revision_id}",
        base(s, other) + f"/revisions/{saved.revision_id}",
        path + f"/revisions/{uuid4()}",
    ]
    statements = []

    def capture(conn, cursor, statement, params, context, many):
        statements.append(statement)

    before = snapshot(s)
    event.listen(s["engine"], "before_cursor_execute", capture)
    try:
        for url in paths:
            denied = client.get(url)
            assert denied.status_code == 404 and denied.json()["detail"]["code"] == "not_found"
        for query in ("limit=0", "limit=201", "offset=-1", "offset=100001"):
            assert client.get(path + "/history?" + query).status_code == 422
        with pytest.raises(LedgerError) as denied:
            BusinessDocumentHistory(s["engine"]).get_revision(
                uuid4(), s["ledger"], doc.id, saved.revision_id
            )
        assert denied.value.code == "not_found"
        client.cookies.clear()
        for suffix in ("/history", f"/revisions/{saved.revision_id}"):
            assert client.get(path + suffix).status_code == 401
    finally:
        event.remove(s["engine"], "before_cursor_execute", capture)
    assert not any("coinpup_confirmed_document_revision_response" in q for q in statements)
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "fault", ["missing", "hash", "status", "anchor", "float", "uuid", "amount"]
)
def test_bad_stored_receipt_is_not_silently_rebuilt(
    drafts, authenticated_client, monkeypatch, fault
):
    s = drafts
    client, _, _ = authenticated_client
    doc, _ = confirmed(s, "bill", 1)
    saved = send(s, doc, correction(doc), "fictional-history-receipt")
    original = Session.get

    def corrupt(self, entity, ident, *args, **kwargs):
        row = original(self, entity, ident, *args, **kwargs)
        if entity is not CommandReceipt:
            return row
        if fault == "missing":
            return None
        fake = SimpleNamespace(
            hash_version=row.hash_version,
            response_status=row.response_status,
            operation_id=row.operation_id,
            response=deepcopy(row.response),
        )
        if fault == "hash":
            fake.hash_version = 1
        if fault == "status":
            fake.response_status = 201
        if fault == "anchor":
            fake.operation_id = uuid4()
        if fault == "float":
            fake.response["version"] = float(saved.version)
        if fault == "uuid":
            fake.response["revision_id"] = str(saved.revision_id).replace("-", "").upper()
        if fault == "amount":
            fake.response["replacement"]["lines"][0]["net_amount"] = "42.00"
        return fake

    before = snapshot(s)
    with monkeypatch.context() as patch:
        patch.setattr(Session, "get", corrupt)
        response = client.get(base(s, doc) + f"/revisions/{saved.revision_id}")
    assert (
        response.status_code == 503
        and response.json()["detail"]["code"] == "document_revision_integrity"
    )
    assert snapshot(s) == before


def test_history_directory_snapshot_does_not_mix_a_concurrent_commit(drafts):
    s = drafts
    doc, _ = confirmed(s, "invoice", 1)
    reads = BusinessDocumentHistory(s["engine"])
    old = reads.list_history(s["owner"], s["ledger"], doc.id)
    thread, done = get_ident(), []

    def after_origin(conn, cursor, statement, params, context, many):
        if get_ident() == thread and not done and "JOIN business_confirmations" in statement:
            done.append(True)
            with ThreadPoolExecutor(max_workers=1) as pool:
                done.append(
                    pool.submit(send, s, doc, correction(doc), "fictional-history-race").result(
                        timeout=30
                    )
                )

    event.listen(s["engine"], "after_cursor_execute", after_origin)
    try:
        during = reads.list_history(s["owner"], s["ledger"], doc.id)
    finally:
        event.remove(s["engine"], "after_cursor_execute", after_origin)
    assert len(done) == 2 and during == old
    assert len(reads.list_history(s["owner"], s["ledger"], doc.id)) == 2


def test_directory_line_counts_include_zero_sources_without_changing_fixed_receipts(drafts):
    s = drafts
    doc, _ = confirmed(s, "invoice", 4)
    saved = send(s, doc, correction(doc), "fictional-zero-count-history")
    before = snapshot(s)
    reads = BusinessDocumentHistory(s["engine"])
    rows = counted(s, lambda: reads.list_history(s["owner"], s["ledger"], doc.id), 5)
    assert rows[-1].previous_line_count == len(saved.previous.lines) == 4
    assert rows[-1].replacement_line_count == len(saved.replacement.lines) == 4
    assert sum(row.posting is None for row in saved.previous.lines) == 2
    assert reads.get_revision(s["owner"], s["ledger"], doc.id, saved.revision_id) == saved
    assert snapshot(s) == before
