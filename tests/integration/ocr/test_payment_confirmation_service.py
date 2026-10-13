"""Fictional bank evidence confirms an existing payment without another financial fact."""

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from coinpup_api.business.schemas import PartyUpdate
from coinpup_api.files.models import OperationFileLink
from coinpup_api.files.schemas import FileUpdate
from coinpup_api.files.service import DocumentService
from coinpup_api.files.storage import FileStore
from coinpup_api.ledger.schemas import AccountUpdate, AssetUpdate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from coinpup_api.ocr.candidates import completion_from_result
from coinpup_api.ocr.confirmation import ConfirmationService
from coinpup_api.ocr.confirmation_contracts import ConfirmationCreate
from coinpup_api.ocr.confirmation_reads import ConfirmationReadService
from coinpup_api.ocr.contracts import JobCreate
from coinpup_api.ocr.models import OcrConfirmation
from coinpup_api.ocr.queue import OcrQueueService
from coinpup_api.ocr.review import DraftReviewService
from coinpup_api.ocr.transactions import BoundCommands
from sqlalchemy import event, func, select

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_settlement_service import pay, prepared
from tests.integration.files.test_files_service import complete, reserve
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.unit.ocr.test_candidates import result
from tests.unit.ocr.test_field_parser import page

pytestmark = pytest.mark.integration
ALLOWED = {"ocr_confirmations", "ocr_drafts", "operation_file_links", "change_log"}


def setup(s, tmp_path, *, count=1):
    payload, _ = prepared(s, count=count)
    payload = payload.model_copy(update={"fees": []})
    payment = pay(s, payload)
    files = s | dict(
        service=DocumentService(s["engine"]), store=FileStore(tmp_path / "private", 1024, 5)
    )
    upload, _ = reserve(files)
    file = complete(files, upload).file_id
    queue = OcrQueueService(s["engine"])
    queue.create_job(
        s["owner"],
        s["ledger"],
        JobCreate(intent_id=uuid4(), file_id=file),
        configuration={"lease_seconds": 120, "retry_seconds": 0, "processing": {}},
    )
    lines = ["Date | Currency | Amount", f"2026-09-15 | USD | {payload.amount}"]
    completed = queue.finish(
        queue.claim(s["owner"]),
        completion_from_result(
            result(
                page(lines, text="\n".join(lines)),
                ocr=(0,),
            )
        ),
    )
    draft = completed.result.draft_ids[0]
    review = DraftReviewService(s["engine"]).get_review(s["owner"], s["ledger"], draft)
    assert review.fields and all(field.requires_confirmation for field in review.fields)
    body = ConfirmationCreate(
        intent_id=uuid4(),
        expected_version=1,
        target_ledger_id=s["ledger"],
        target_file_id=file,
        confirmed=[field.path for field in review.fields],
        entry=dict(kind="link_payment", payment_id=payment.id, expected_version=1),
    )
    return s | dict(
        files=files["service"],
        file=file,
        draft=draft,
        body=body,
        payment=payment,
        confirmer=ConfirmationService(s["engine"]),
    )


def confirm(s, body=None, *, raw=None):
    return s["confirmer"].confirm(
        s["owner"], s["ledger"], s["draft"], body or s["body"], raw_body=raw
    )


@pytest.mark.parametrize("count", [1, 200])
def test_complete_original_payment_links_once_and_raw_replay_changes_no_table(
    drafts, tmp_path, count
):
    s = setup(drafts, tmp_path, count=count)
    before = snapshot(s)
    raw = json.dumps(s["body"].model_dump(mode="json", exclude_unset=True), indent=2).encode()
    receipt = confirm(s, raw=raw)
    after = snapshot(s)
    assert receipt.action == "link_payment" and receipt.payment == s["payment"]
    assert "operation" not in receipt.model_dump() and receipt.operation_version == 1
    assert {k: v for k, v in after.items() if k not in ALLOWED} == {
        k: v for k, v in before.items() if k not in ALLOWED
    }
    assert confirm(s, raw=raw).model_dump_json() == receipt.model_dump_json()
    assert (
        ConfirmationReadService(s["engine"]).get_confirmation(s["owner"], s["ledger"], s["draft"])
        == receipt
    )
    assert snapshot(s) == after
    with s["engine"].connect() as c:
        assert c.scalar(select(func.count()).select_from(OperationFileLink)) == 1
        assert c.scalar(select(OcrConfirmation.payment_id)) == s["payment"].id


def test_new_intent_requires_all_ocr_fields_and_exact_version_without_writes(drafts, tmp_path):
    s = setup(drafts, tmp_path)
    for changes, code in [
        ({"confirmed": []}, "ocr_review_required"),
        ({"expected_version": 2}, "version_conflict"),
        (
            {"entry": dict(kind="link_payment", payment_id=s["payment"].id, expected_version=2)},
            "version_conflict",
        ),
        ({"entry": dict(kind="link_payment", payment_id=uuid4(), expected_version=1)}, "not_found"),
        ({"target_file_id": uuid4()}, "not_found"),
    ]:
        before = snapshot(s)
        body = ConfirmationCreate.model_validate(s["body"].model_dump() | changes)
        with pytest.raises(LedgerError) as error:
            confirm(s, body)
        assert error.value.code == code and snapshot(s) == before


def test_same_raw_intent_is_permanent_after_close_disable_and_archive(drafts, tmp_path):
    s = setup(drafts, tmp_path)
    raw = s["body"].model_dump_json(exclude_unset=True).encode()
    receipt = confirm(s, raw=raw)
    change_period(s, cutoff="2026-09-30")
    s["files"].update_file(
        s["owner"], s["ledger"], s["file"], FileUpdate(expected_version=1, archived=True)
    )
    s["master"].update_party(
        s["owner"], s["ledger"], s["party"].id, PartyUpdate(expected_version=1, archived=True)
    )
    s["structure"].update_account(
        s["owner"], s["ledger"], s["account"].id, AccountUpdate(expected_version=1, archived=True)
    )
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    statements = []

    def capture(connection, cursor, sql, *args):
        statements.append(sql.lower())

    event.listen(s["engine"], "before_cursor_execute", capture)
    try:
        assert confirm(s, raw=raw) == receipt
    finally:
        event.remove(s["engine"], "before_cursor_execute", capture)
    assert not any("from financial_operations" in sql for sql in statements)
    assert snapshot(s) == before
    changed = json.loads(raw) | {"duplicate_ack": False}
    with pytest.raises(LedgerError) as error:
        confirm(s, ConfirmationCreate.model_validate(changed), raw=json.dumps(changed).encode())
    assert error.value.code == "idempotency_conflict" and snapshot(s) == before


def test_concurrent_same_intent_commits_one_association_and_original_receipt(drafts, tmp_path):
    s = setup(drafts, tmp_path)
    before = snapshot(s)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: confirm(s), range(2)))
    assert receipts[0] == receipts[1]
    after = snapshot(s)
    assert len(after["ocr_confirmations"]) == len(after["operation_file_links"]) == 1
    assert {k: v for k, v in after.items() if k not in ALLOWED} == {
        k: v for k, v in before.items() if k not in ALLOWED
    }


def test_closed_financial_period_accepts_only_nonfinancial_evidence_changes(drafts, tmp_path):
    s = setup(drafts, tmp_path)
    change_period(s, cutoff="2026-09-30")
    before = snapshot(s)
    assert confirm(s).payment == s["payment"]
    after = snapshot(s)
    assert {k: v for k, v in after.items() if k not in ALLOWED} == {
        k: v for k, v in before.items() if k not in ALLOWED
    }


def test_late_link_failure_rolls_back_confirmation_draft_link_and_notifications(
    drafts, tmp_path, monkeypatch
):
    s = setup(drafts, tmp_path)
    before = snapshot(s)
    original = BoundCommands.link

    def fail(self, *args):
        original(self, *args)
        raise RuntimeError("Fictional late evidence failure")

    monkeypatch.setattr(BoundCommands, "link", fail)
    with pytest.raises(RuntimeError, match="Fictional late"):
        confirm(s)
    assert snapshot(s) == before
