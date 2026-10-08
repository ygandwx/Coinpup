"""Real authenticated HTTP confirmation, replay and bounded private duplicate reads."""

import json
from uuid import uuid4

import pytest
from coinpup_api.files.schemas import LinkUpdate
from coinpup_api.ledger.posting_schemas import CancellationCreate
from coinpup_api.ledger.service import LedgerError
from coinpup_api.ocr.confirmation_reads import ConfirmationReadService
from sqlalchemy import event

from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ocr.test_bound_transactions import bound_setup as bound_setup
from tests.integration.ocr.test_bound_transactions import payload
from tests.integration.ocr.test_confirmation_schema import snapshot
from tests.integration.ocr.test_confirmation_service import confirm_setup as confirm_setup
from tests.integration.ocr.test_confirmation_service import recognized, request

pytestmark = pytest.mark.integration


def prefix(s):
    return f"/api/v1/ledgers/{s['ledger']}/ocr-drafts/{s['draft']}"


def test_http_original_request_replays_after_lost_reply_and_reads_permanent_receipt(
    confirm_setup, authenticated_client
):
    s = confirm_setup
    client, _, owner = authenticated_client
    assert owner == s["owner"]
    raw = request(s).model_dump_json(exclude_unset=True)
    result = client.post(
        prefix(s) + "/confirmations", content=raw, headers={"content-type": "application/json"}
    )
    assert result.status_code == 201 and result.headers["cache-control"] == "no-store"
    before = snapshot(s)
    # Deliberately ignore the first response's contents when deciding what to resend.
    repeated = client.post(
        prefix(s) + "/confirmations", content=raw, headers={"content-type": "application/json"}
    )
    assert repeated.status_code == 201 and repeated.content == result.content
    assert client.get(prefix(s) + "/confirmation").json() == result.json()
    assert client.get(prefix(s) + "/review").json()["status"] == "confirmed"
    assert snapshot(s) == before
    changed = json.loads(raw) | {"duplicate_ack": False}
    conflict = client.post(prefix(s) + "/confirmations", json=changed)
    assert (
        conflict.status_code == 409 and conflict.json()["detail"]["code"] == "idempotency_conflict"
    )
    assert snapshot(s) == before


def test_duplicate_reads_are_scoped_readonly_and_source_rows_are_not_auto_merged(
    confirm_setup, authenticated_client
):
    s = confirm_setup
    client, _, _ = authenticated_client
    assert client.get(prefix(s) + "/confirmation").status_code == 404
    assert client.get(prefix(s) + "/duplicates").json() == []
    first = client.post(
        prefix(s) + "/confirmations", json=request(s).model_dump(mode="json", exclude_unset=True)
    ).json()
    original_draft = s["draft"]
    s["draft"] = recognized(s)
    before, statements = snapshot(s), []

    def sql(_connection, _cursor, text, _parameters, _context, _many):
        statements.append(text.lower())

    event.listen(s["engine"], "before_cursor_execute", sql)
    try:
        duplicates = client.get(prefix(s) + "/duplicates")
    finally:
        event.remove(s["engine"], "before_cursor_execute", sql)
    assert duplicates.status_code == 200 and len(duplicates.json()) == 1
    row = duplicates.json()[0]
    assert (
        row["draft_id"] == str(original_draft) and row["operation_id"] == first["operation"]["id"]
    )
    assert set(row) == {"draft_id", "ledger_id", "target_ledger_id", "operation_id", "created_at"}
    assert all("for update" not in q and "pg_advisory" not in q for q in statements)
    assert snapshot(s) == before
    blocked = client.post(
        prefix(s) + "/confirmations", json=request(s).model_dump(mode="json", exclude_unset=True)
    )
    assert (
        blocked.status_code == 409
        and blocked.json()["detail"]["code"] == "ocr_duplicate_confirmation"
    )
    assert snapshot(s) == before
    reads = ConfirmationReadService(s["engine"])
    for owner, ledger in [(uuid4(), s["ledger"]), (s["owner"], uuid4())]:
        with pytest.raises(LedgerError) as error:
            reads.list_duplicates(owner, ledger, s["draft"])
        assert error.value.code == "not_found"


@pytest.mark.parametrize(
    "problem,code",
    [
        ("stale", "version_conflict"),
        ("cancelled", "operation_cancelled"),
        ("archived_link", "file_link_archived"),
    ],
)
def test_http_link_refuses_stale_cancelled_or_archived_target_without_side_effects(
    confirm_setup, authenticated_client, problem, code
):
    s = confirm_setup
    client, _, _ = authenticated_client
    operation = s["posting"].post_income(
        s["owner"], s["ledger"], payload(s, "income"), "fictional-target"
    )
    expected = 2 if problem == "stale" else 1
    if problem == "cancelled":
        s["posting"].cancel_operation(
            s["owner"],
            s["ledger"],
            operation.id,
            CancellationCreate(expected_version=1, reason="Fictional cancellation"),
            "fictional-cancel",
        )
        expected = 2
    if problem == "archived_link":
        s["service"].link_file(s["owner"], s["ledger"], operation.id, s["file"])
        s["service"].update_link(
            s["owner"],
            s["ledger"],
            operation.id,
            s["file"],
            LinkUpdate(expected_version=1, archived=True),
        )
    body = request(
        s, entry={"kind": "link", "operation_id": operation.id, "expected_version": expected}
    )
    before = snapshot(s)
    response = client.post(
        prefix(s) + "/confirmations", json=body.model_dump(mode="json", exclude_unset=True)
    )
    assert response.status_code == 409 and response.json()["detail"]["code"] == code
    assert snapshot(s) == before
