"""Fictional human confirmations reuse original financial semantics and permanent intents."""

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from coinpup_api.files.schemas import FileUpdate
from coinpup_api.ledger.idempotency import command_hash_v2
from coinpup_api.ledger.models import CommandReceipt
from coinpup_api.ledger.posting_schemas import CancellationCreate
from coinpup_api.ledger.schemas import AccountCreate, EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from coinpup_api.ocr.candidates import completion_from_result
from coinpup_api.ocr.confirmation import ConfirmationService
from coinpup_api.ocr.confirmation_contracts import ConfirmationCreate
from coinpup_api.ocr.contracts import JobCreate
from coinpup_api.ocr.models import OcrConfirmation
from coinpup_api.ocr.queue import OcrQueueService
from coinpup_api.ocr.review import DraftReviewService
from coinpup_api.ocr.transactions import BoundCommands
from sqlalchemy import event, select

from tests.integration.files.test_files_service import OTHER_PDF, PDF, complete, reserve
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ledger.test_posting_service_database import opening
from tests.integration.ocr.test_bound_transactions import bound_setup as bound_setup
from tests.integration.ocr.test_bound_transactions import payload
from tests.integration.ocr.test_confirmation_schema import snapshot
from tests.integration.ocr.test_ocr_queue import finances
from tests.unit.ocr.test_candidates import statement

pytestmark = pytest.mark.integration


def recognized(s):
    queue = OcrQueueService(s["engine"])
    queue.create_job(
        s["owner"],
        s["ledger"],
        JobCreate(intent_id=uuid4(), file_id=s["file"]),
        configuration={"lease_seconds": 120, "retry_seconds": 0, "processing": {}},
    )
    return queue.finish(
        queue.claim(s["owner"]), completion_from_result(statement("10.00", ocr=(0,)))
    ).result.draft_ids[0]


@pytest.fixture
def confirm_setup(bound_setup):
    s = bound_setup
    s["draft"] = recognized(s)
    s["confirmer"] = ConfirmationService(s["engine"])
    return s


def request(s, kind="income", **changes):
    review = DraftReviewService(s["engine"]).get_review(s["owner"], s["ledger"], s["draft"])
    return ConfirmationCreate.model_validate(
        dict(
            intent_id=uuid4(),
            expected_version=1,
            target_ledger_id=s["ledger"],
            target_file_id=s["file"],
            confirmed=[f.path for f in review.fields],
            entry={
                "kind": kind,
                "command": payload(s, kind).model_dump(mode="json", exclude_unset=True),
            },
        )
        | changes
    )


def confirm(s, body, *, raw=None):
    return s["confirmer"].confirm(s["owner"], s["ledger"], s["draft"], body, raw_body=raw)


@pytest.mark.parametrize("kind", ["opening", "income", "expense", "transfer", "exchange"])
def test_all_existing_commands_confirm_once_and_replay_exactly(confirm_setup, kind):
    s = confirm_setup
    body = request(s, kind)
    result = confirm(s, body)
    assert result.operation.kind == kind and result.draft_version == 2
    after = snapshot(s)
    assert confirm(s, body).model_dump_json() == result.model_dump_json()
    assert snapshot(s) == after
    detail = DraftReviewService(s["engine"]).get_review(s["owner"], s["ledger"], s["draft"])
    assert detail.status == "confirmed" and detail.review.entry["kind"] == kind
    with s["engine"].connect() as connection:
        receipt = connection.execute(select(OcrConfirmation.__table__)).mappings().one()
        financial_hash = connection.scalar(select(CommandReceipt.request_hash))
    assert receipt["operation_id"] == result.operation.id and receipt["hash_version"] == 2
    assert financial_hash == command_hash_v2(kind, s["ledger"], body.entry.command)


def test_link_existing_adds_evidence_without_posting_money(confirm_setup):
    s = confirm_setup
    operation = s["posting"].post_income(
        s["owner"], s["ledger"], payload(s, "income"), "fictional-existing"
    )
    before = finances(s["engine"])
    result = confirm(
        s, request(s, entry={"kind": "link", "operation_id": operation.id, "expected_version": 1})
    )
    assert result.action == "link" and result.operation == operation
    assert finances(s["engine"]) == before


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"confirmed": []}, "ocr_review_required"),
        ({"confirmed": ["fictional-unknown"]}, "ocr_invalid_review"),
        ({"expected_version": 2}, "version_conflict"),
        ({"target_file_id": uuid4()}, "not_found"),
        ({"target_ledger_id": uuid4()}, "not_found"),
    ],
)
def test_invalid_confirmation_writes_nothing(confirm_setup, changes, code):
    s = confirm_setup
    body, before = request(s, **changes), snapshot(s)
    with pytest.raises(LedgerError) as error:
        confirm(s, body)
    assert error.value.code == code and snapshot(s) == before


@pytest.mark.parametrize("failure", ["after_financial", "after_link", "receipt_flush"])
def test_every_late_failure_rolls_back_finance_receipts_links_draft_and_log(
    confirm_setup, monkeypatch, failure
):
    s = confirm_setup
    body, before = request(s), snapshot(s)
    original = BoundCommands.link

    def link(self, *args):
        if failure != "after_financial":
            original(self, *args)
        raise RuntimeError("Fictional late confirmation failure")

    def flush(_connection, _cursor, sql, _parameters, _context, _many):
        if sql.startswith("INSERT INTO ocr_confirmations"):
            raise RuntimeError("Fictional late confirmation failure")

    if failure == "receipt_flush":
        event.listen(s["engine"], "before_cursor_execute", flush)
    else:
        monkeypatch.setattr(BoundCommands, "link", link)
    try:
        with pytest.raises(RuntimeError, match="Fictional late"):
            confirm(s, body)
    finally:
        if failure == "receipt_flush":
            event.remove(s["engine"], "before_cursor_execute", flush)
    assert snapshot(s) == before


def test_concurrent_distinct_intents_for_one_draft_cannot_double_post(confirm_setup):
    s = confirm_setup
    bodies = [request(s), request(s)]

    def run(body):
        try:
            return confirm(s, body)
        except LedgerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, bodies))
    assert results.count("version_conflict") == 1
    assert sum(not isinstance(result, str) for result in results) == 1
    assert len(snapshot(s)[1]) == 1


def test_new_recognition_of_same_source_requires_explicit_duplicate_choice(confirm_setup):
    s = confirm_setup
    first = confirm(s, request(s))
    s["draft"] = recognized(s)
    body, before = request(s), snapshot(s)
    with pytest.raises(LedgerError) as error:
        confirm(s, body)
    assert error.value.code == "ocr_duplicate_confirmation" and snapshot(s) == before
    changed = body.model_copy(update={"duplicate_ack": True})
    second = confirm(s, changed)
    assert second.operation.id != first.operation.id


def test_original_raw_request_replays_after_cancellation_and_archive(confirm_setup):
    s = confirm_setup
    value = request(s).model_dump(mode="json", exclude_unset=True)
    value["entry"]["command"].pop("description")
    body = ConfirmationCreate.model_validate(value)
    raw = json.dumps(value).encode()
    result = confirm(s, body, raw=raw)
    for change in ({"duplicate_ack": False}, {"confirmed": list(reversed(value["confirmed"]))}):
        changed = value | change
        with pytest.raises(LedgerError) as error:
            confirm(s, ConfirmationCreate.model_validate(changed), raw=json.dumps(changed).encode())
        assert error.value.code == "idempotency_conflict"
    s["posting"].cancel_operation(
        s["owner"],
        s["ledger"],
        result.operation.id,
        CancellationCreate(expected_version=1, reason="Fictional cancellation"),
        "fictional-cancel",
    )
    s["service"].update_file(
        s["owner"], s["ledger"], s["file"], FileUpdate(expected_version=1, archived=True)
    )
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    assert confirm(s, body, raw=raw).model_dump_json() == result.model_dump_json()
    assert snapshot(s) == before


@pytest.mark.parametrize("copy_kind", ["same", "different", "cross_reference"])
def test_cross_ledger_requires_a_separately_uploaded_identical_original(confirm_setup, copy_kind):
    s = confirm_setup
    target = (
        s["structure"]
        .create_entity(
            s["owner"],
            EntityCreate(
                kind="personal",
                name="Fictional second owner ledger",
                base_asset_id="USD",
            ),
        )
        .ledger.id
    )
    account = s["structure"].create_account(
        s["owner"],
        target,
        AccountCreate(
            name="Fictional target bank",
            kind="bank",
            asset_ids=["USD"],
        ),
    )
    content = OTHER_PDF if copy_kind == "different" else PDF
    upload, _ = reserve(s, ledger=target, content=content)
    file = complete(s, upload, ledger=target, content=content).file_id
    command = opening(s | {"account": account}).model_dump(mode="json", exclude_unset=True)
    body = request(
        s,
        target_ledger_id=target,
        target_file_id=s["file"] if copy_kind == "cross_reference" else file,
        entry={"kind": "opening", "command": command},
    )
    before = snapshot(s)
    if copy_kind == "same":
        response = confirm(s, body)
        assert response.ledger_id == s["ledger"] and response.operation.ledger_id == target
        assert response.target_file_id == file and file != s["file"]
    else:
        with pytest.raises(LedgerError) as error:
            confirm(s, body)
        assert error.value.code == (
            "ocr_source_mismatch" if copy_kind == "different" else "not_found"
        )
        assert snapshot(s) == before
