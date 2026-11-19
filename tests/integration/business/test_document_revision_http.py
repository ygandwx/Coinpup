"""Actual whole-document HTTP commits retain original evidence and unknown-command recovery."""

import json
from uuid import uuid4

import pytest
from coinpup_api.business.document_revision_receipt import DocumentRevisionResponse
from coinpup_api.business.document_revision_schemas import ConfirmedDocumentCancellation
from coinpup_api.business.document_revisions import BusinessDocumentRevisionService
from coinpup_api.ledger.schemas import AssetUpdate, EntityCreate, EntityUpdate
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError

from tests.integration.business.test_document_revision_capacity import replacement
from tests.integration.business.test_document_revision_commands import correction
from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def frozen(s, doc, payload, key):
    action = (
        "cancellations" if isinstance(payload, ConfirmedDocumentCancellation) else "corrections"
    )
    return (
        f"/api/v1/ledgers/{s['ledger']}/business-documents/{doc.id}/{action}",
        json.dumps(payload.model_dump(mode="json", exclude_unset=True), indent=2).encode(),
        {"Content-Type": "application/json", "Idempotency-Key": key},
    )


def post(client, intent):
    path, raw, headers = intent
    result = client.post(path, content=raw, headers=headers)
    assert result.headers["cache-control"] == "no-store"
    return result


@pytest.mark.parametrize("kind", ["invoice", "bill"])
@pytest.mark.parametrize("count", [1, 200])
def test_complete_http_correction_and_cancel_keep_original_confirmation_and_all_receipts(
    drafts, authenticated_client, kind, count
):
    s = drafts
    client, engine, owner = authenticated_client
    assert engine is s["engine"] and owner == s["owner"]
    doc, _ = confirmed(s, kind, count)
    original_path = f"/api/v1/ledgers/{s['ledger']}/business-documents/{doc.id}/confirmation"
    original = client.get(original_path).json()
    payload = correction(doc).model_copy(
        update={"replacement": replacement(doc, {r.id: {"unit_price": "0.02"} for r in doc.lines})}
    )
    intent = frozen(s, doc, payload, "fictional-http-document-correct")
    response = post(client, intent)
    assert response.status_code == 200, response.text
    fixed = DocumentRevisionResponse.model_validate(response.json())
    assert fixed.version == doc.version + 1 and len(fixed.reversals) == count
    assert [r.id for r in fixed.replacement.lines] == [r.id for r in doc.lines]
    ending = frozen(
        s,
        doc,
        ConfirmedDocumentCancellation(
            expected_version=fixed.version, reason="Fictional complete HTTP cancellation"
        ),
        "fictional-http-document-cancel",
    )
    response = post(client, ending)
    assert response.status_code == 200, response.text
    ended = DocumentRevisionResponse.model_validate(response.json())
    assert ended.version == fixed.version + 1 and ended.replacement is None
    assert len(ended.reversals) == count
    change_period(s, cutoff="2026-11-06")
    s["structure"].update_asset(owner, "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        owner, s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    assert post(client, intent).json() == fixed.model_dump(mode="json")
    assert post(client, ending).json() == ended.model_dump(mode="json")
    assert client.get(original_path).json() == original
    changed = (
        intent[0],
        json.dumps(payload.model_dump(mode="json") | {"reason": "Fictional new intent"}).encode(),
        intent[2],
    )
    conflict = post(client, changed)
    assert (
        conflict.status_code == 409 and conflict.json()["detail"]["code"] == "idempotency_conflict"
    )
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "fault,status,code",
    [
        ("stale", 409, "version_conflict"),
        ("precision", 422, "amount_precision"),
        ("closed", 409, "period_closed"),
        ("other_ledger", 404, "not_found"),
        ("owner_field", 422, None),
    ],
)
def test_invalid_real_http_commands_change_no_table(
    drafts, authenticated_client, fault, status, code
):
    s = drafts
    client, _, _ = authenticated_client
    doc, _ = confirmed(s, "invoice", 1)
    payload = correction(doc)
    if fault == "stale":
        payload = payload.model_copy(update={"expected_version": doc.version + 1})
    if fault == "precision":
        payload = payload.model_copy(
            update={"replacement": replacement(doc, {doc.lines[0].id: {"unit_price": "0.001"}})}
        )
    if fault == "closed":
        change_period(s, cutoff="2026-11-06")
    path, raw, headers = frozen(s, doc, payload, "fictional-http-rejected")
    if fault == "other_ledger":
        other = s["structure"].create_entity(
            s["owner"],
            EntityCreate(
                kind="personal",
                name="Fictional other HTTP scope",
                base_asset_id="USD",
                template_key="personal_default",
            ),
        )
        path = path.replace(str(s["ledger"]), str(other.ledger.id))
    if fault == "owner_field":
        raw = json.dumps(json.loads(raw) | {"owner_id": str(uuid4())}).encode()
    before = snapshot(s)
    response = post(client, (path, raw, headers))
    assert response.status_code == status, response.text
    if code:
        assert response.json()["detail"]["code"] == code
    assert snapshot(s) == before


@pytest.mark.parametrize("action", ["correct", "cancel"])
def test_lost_committed_response_retries_the_same_http_intent_without_any_write(
    drafts, authenticated_client, monkeypatch, action
):
    s = drafts
    client, _, _ = authenticated_client
    doc, _ = confirmed(s, "invoice", 1)
    payload = (
        correction(doc)
        if action == "correct"
        else ConfirmedDocumentCancellation(
            expected_version=doc.version, reason="Fictional lost cancellation response"
        )
    )
    intent = frozen(s, doc, payload, "fictional-http-unknown-document")
    method = getattr(BusinessDocumentRevisionService, action)
    saved = []

    def lost(self, *args, **kwargs):
        saved.append(method(self, *args, **kwargs))
        raise SQLAlchemyError("Fictional connection lost after committed response")

    with monkeypatch.context() as patch:
        patch.setattr(BusinessDocumentRevisionService, action, lost)
        response = post(client, intent)
    assert response.status_code == 503 and len(saved) == 1
    before = snapshot(s)
    response = post(client, intent)
    assert response.status_code == 200 and response.json() == saved[0].model_dump(mode="json")
    assert snapshot(s) == before


def test_mid_transaction_http_failure_rolls_back_the_group_before_same_intent_retry(
    drafts, authenticated_client
):
    s = drafts
    client, _, _ = authenticated_client
    doc, _ = confirmed(s, "invoice", 4)
    intent = frozen(s, doc, correction(doc), "fictional-http-atomic-document")
    before = snapshot(s)

    def fail(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().startswith("INSERT INTO business_document_accruals "):
            raise SQLAlchemyError("Fictional private failure after document member insert")

    event.listen(s["engine"], "after_cursor_execute", fail)
    try:
        response = post(client, intent)
    finally:
        event.remove(s["engine"], "after_cursor_execute", fail)
    assert response.status_code == 503 and "private" not in response.text
    assert snapshot(s) == before
    result = post(client, intent)
    assert result.status_code == 200, result.text
    before = snapshot(s)
    assert post(client, intent).json() == result.json() and snapshot(s) == before
