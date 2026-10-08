"""Fictional confirmation evidence must commit atomically and remain permanent."""

from contextlib import contextmanager
from uuid import uuid4

import pytest
from coinpup_api.files.models import OperationFileLink, StoredFile
from coinpup_api.ledger.posting_schemas import CancellationCreate
from coinpup_api.ocr.contracts import Candidate, Completion, JobCreate
from coinpup_api.ocr.draft_reads import DraftReadService
from coinpup_api.ocr.models import OcrConfirmation, OcrDraft
from coinpup_api.ocr.queue import OcrQueueService
from coinpup_api.sync.locking import acquire_write_lock
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.conftest import isolated_confirmation_schema
from tests.integration.ledger.test_posting_service_database import classified, opening
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ocr.test_bound_transactions import bound_setup as bound_setup
from tests.integration.ocr.test_bound_transactions import state

pytestmark = pytest.mark.integration


@pytest.fixture
def confirmation_setup(bound_setup):
    s = bound_setup
    queue = OcrQueueService(s["engine"])
    queue.create_job(
        s["owner"],
        s["ledger"],
        JobCreate(intent_id=uuid4(), file_id=s["file"]),
        configuration={"lease_seconds": 120, "retry_seconds": 0, "processing": {}},
    )
    result = queue.finish(
        queue.claim(s["owner"]),
        Completion(
            candidates=[
                Candidate(
                    source_key="document:0",
                    recognized={"text": "Fictional amount 100.00"},
                    evidence={"pages": [1]},
                    fields={},
                )
            ]
        ),
    )
    operation = s["posting"].post_opening(s["owner"], s["ledger"], opening(s), "fictional-opening")
    s["service"].link_file(s["owner"], s["ledger"], operation.id, s["file"])
    s["confirmation"] = dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        created_by=s["owner"],
        draft_id=result.result.draft_ids[0],
        intent_id=uuid4(),
        source_file_id=s["file"],
        source_key="document:0",
        target_ledger_id=s["ledger"],
        target_file_id=s["file"],
        operation_id=operation.id,
        draft_version=1,
        operation_version=1,
        action="link",
        request_hash="a" * 64,
        review={"fictional_review": "100.00"},
        response={"fictional_operation_id": str(operation.id)},
    )
    return s


def snapshot(s):
    with s["engine"].connect() as connection:
        receipts = connection.execute(select(OcrConfirmation.__table__)).mappings().all()
    return state(s), receipts


@contextmanager
def writer(s):
    with Session(s["engine"]) as session, session.begin():
        acquire_write_lock(session)
        s["structure"]._ledger(session, s["owner"], s["ledger"], write=True)
        yield session


def write(s, *, receipt=True, draft=True, **changes):
    c = s["confirmation"]
    with writer(s) as session:
        s["structure"]._ledger(session, s["owner"], s["ledger"], write=True)
        if receipt:
            session.execute(insert(OcrConfirmation).values(c | changes))
        if draft:
            session.execute(
                update(OcrDraft)
                .where(OcrDraft.id == c["draft_id"])
                .values(
                    status="confirmed",
                    version=2,
                    updated_at=func.clock_timestamp(),
                    fields=c["review"],
                )
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"receipt": False},
        {"draft": False},
        {"draft_version": 2},
        {"operation_version": 2},
        {"source_key": "document:1"},
        {"review": {"fictional_review": "101.00"}},
    ],
)
def test_partial_or_mismatched_confirmation_rolls_back_all_changes(confirmation_setup, changes):
    s = confirmation_setup
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error:
        write(s, **changes)
    assert error.value.orig.sqlstate == "23514"
    assert error.value.orig.diag.constraint_name == "ck_ocr_confirmation_consistency"
    assert snapshot(s) == before


def test_atomic_confirmation_is_readable_notified_and_permanent(confirmation_setup):
    s = confirmation_setup
    before = snapshot(s)
    write(s)
    reader = DraftReadService(s["engine"])
    confirmed = reader.get_draft(s["owner"], s["ledger"], s["confirmation"]["draft_id"])
    assert confirmed.status == "confirmed" and confirmed.version == 2
    assert confirmed.fields == s["confirmation"]["review"]
    assert confirmed.recognized == {"text": "Fictional amount 100.00"}
    assert reader.list_drafts(s["owner"], s["ledger"], status="confirmed")[0].id == confirmed.id
    with s["engine"].connect() as connection:
        events = (
            connection.execute(select(ChangeLog.entity_type).order_by(ChangeLog.seq))
            .scalars()
            .all()
        )
    assert events[-2:] == ["ocr_confirmations", "ocr_drafts"]
    committed = snapshot(s)
    assert committed[0][0] == before[0][0]  # Confirmation itself adds no financial command.
    for statement, constraint in [
        (delete(OcrConfirmation), "ck_ocr_confirmations_immutable"),
        (update(OcrConfirmation).values(version=2, response={}), "ck_ocr_confirmations_immutable"),
        (update(OcrDraft).values(version=3, status="draft"), "ck_ocr_drafts_immutable"),
        (update(OcrDraft).values(version=3, fields={}), "ck_ocr_drafts_immutable"),
    ]:
        with pytest.raises(IntegrityError) as error:
            with writer(s) as session:
                s["structure"]._ledger(session, s["owner"], s["ledger"], write=True)
                session.execute(statement)
        assert error.value.orig.diag.constraint_name == constraint
        assert snapshot(s) == committed


@pytest.mark.parametrize(
    "damage", ["missing_link", "archived_link", "archived_source", "different_bytes"]
)
def test_confirmation_requires_current_matching_original_and_link(confirmation_setup, damage):
    s = confirmation_setup
    if damage == "missing_link":
        unlinked = s["posting"].post_income(
            s["owner"], s["ledger"], classified(s, kind="income"), "fictional-unlinked"
        )
        s["confirmation"]["operation_id"] = unlinked.id
    with writer(s) as session:
        s["structure"]._ledger(session, s["owner"], s["ledger"], write=True)
        if damage == "different_bytes":
            original = session.get(StoredFile, s["file"])
            target = uuid4()
            session.execute(
                insert(StoredFile).values(
                    id=target,
                    ledger_id=s["ledger"],
                    created_by=s["owner"],
                    blob_key=uuid4().hex,
                    sha256="b" * 64,
                    byte_size=original.byte_size,
                    detected_media_type=original.detected_media_type,
                    original_filename="fictional-other.pdf",
                    title="Fictional other original",
                )
            )
            s["confirmation"]["target_file_id"] = target
        elif damage != "missing_link":
            model = StoredFile if damage == "archived_source" else OperationFileLink
            session.execute(
                update(model).values(archived=True, version=2, updated_at=func.clock_timestamp())
            )
    if damage == "different_bytes":
        s["service"].link_file(s["owner"], s["ledger"], s["confirmation"]["operation_id"], target)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error:
        write(s)
    assert error.value.orig.diag.constraint_name == "ck_ocr_confirmation_consistency"
    assert snapshot(s) == before


def test_later_cancellation_preserves_original_confirmation(confirmation_setup):
    s = confirmation_setup
    write(s)
    before = snapshot(s)[1]
    s["posting"].cancel_operation(
        s["owner"],
        s["ledger"],
        s["confirmation"]["operation_id"],
        CancellationCreate(expected_version=1, reason="Fictional cancellation"),
        "fictional-cancel",
    )
    assert snapshot(s)[1] == before


def test_confirmation_history_blocks_downgrade(confirmation_setup):
    s = confirmation_setup
    write(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error:
        isolated_confirmation_schema(s["engine"], "downgrade")
    assert error.value.orig.diag.constraint_name == "ck_ocr_confirmation_downgrade"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "change",
    [
        {"hash_version": 1},
        {"request_hash": "fictional-invalid"},
        {"action": "replace"},
    ],
)
def test_confirmation_receipt_contract_is_enforced_in_database(confirmation_setup, change):
    s = confirmation_setup
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error:
        write(s, **change)
    assert error.value.orig.sqlstate == "23514"
    assert snapshot(s) == before


def test_second_confirmation_cannot_reuse_draft_or_intent(confirmation_setup):
    s = confirmation_setup
    write(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error:
        write(s, draft=False, id=uuid4())
    assert error.value.orig.sqlstate == "23505"
    assert snapshot(s) == before


def test_empty_migration_round_trip_preserves_existing_drafts(confirmation_setup):
    s = confirmation_setup
    before = state(s)
    isolated_confirmation_schema(s["engine"], "downgrade")
    isolated_confirmation_schema(s["engine"], "upgrade")
    assert state(s) == before
    with s["engine"].connect() as connection:
        definition = connection.exec_driver_sql(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_change_log_entity_type'"
        ).scalar_one()
        for entity_type in (
            "business_parties",
            "business_documents",
            "business_document_lines",
            "ledger_periods",
            "ledger_period_audits",
            "ledger_period_receipts",
        ):
            assert entity_type in definition
    write(s)  # Recreated deferred guards accept the same valid atomic confirmation.
