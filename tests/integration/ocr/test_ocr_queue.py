"""Real PostgreSQL queue races use fictional metadata, never an OCR benchmark."""

from concurrent.futures import Future
from dataclasses import replace
from threading import Event, Thread, current_thread
from time import monotonic
from uuid import uuid4

import pytest
from coinpup_api.files.schemas import FileUpdate
from coinpup_api.files.service import DocumentService
from coinpup_api.ledger.models import (
    CommandReceipt,
    FinancialOperation,
    Journal,
    JournalLine,
    OpeningPosition,
)
from coinpup_api.ledger.schemas import EntityUpdate
from coinpup_api.ledger.service import LedgerError
from coinpup_api.ocr.contracts import Candidate, Completion, JobCreate
from coinpup_api.ocr.models import OcrDraft, OcrJob
from coinpup_api.ocr.queue import OcrQueueService
from coinpup_api.sync.service import ChangeService
from sqlalchemy import create_engine, event, func, insert, select, text, update

from tests.integration.ocr.test_ocr_schema import ocr_structure as ocr_structure
from tests.integration.ocr.test_ocr_schema import rows, writer

pytestmark = pytest.mark.integration


@pytest.fixture
def queue_structure(ocr_structure):
    s = ocr_structure
    # Bound database waits independently of Python's thread join timeout.
    bounded = create_engine(s["engine"].url, connect_args={"options": "-c statement_timeout=5000"})
    s["queue_engine"] = bounded
    s["queue"] = OcrQueueService(bounded)
    s["configuration"] = {
        "lease_seconds": 120,
        "retry_seconds": 0,
        "processing": {"fixture": "Fictional processor contract", "revision": 1},
    }
    try:
        yield s
    finally:
        bounded.dispose()


def create(s, *, ledger=0, intent=None, configuration=None):
    return s["queue"].create_job(
        s["owner"],
        s["ledgers"][ledger],
        JobCreate(intent_id=intent or uuid4(), file_id=s["files"][ledger]),
        configuration=s["configuration"] if configuration is None else configuration,
    )


def completion(*, second=False):
    candidates = [
        Candidate(
            source_key="page:1/line:1",
            recognized={"amount": "12.30", "currency": "USD"},
            evidence={"page": 1, "text": "Fictional callback receipt"},
            fields={"amount": "12.30"},
        )
    ]
    if second:
        candidates.append(candidates[0].model_copy(update={"source_key": "page:1/line:2"}))
    return Completion(
        summary={
            "pages": 1,
            "manual_pages": 0,
            "fixture": "Fictional callback, no recognition performed",
        },
        candidates=tuple(candidates),
    )


def expire(s, lease):
    with writer(s) as session:
        record = session.get(OcrJob, lease.job_id)
        session.execute(
            update(OcrJob)
            .where(OcrJob.id == lease.job_id)
            .values(
                version=record.version + 1,
                updated_at=func.clock_timestamp(),
                lease_until=func.clock_timestamp() - text("INTERVAL '1 second'"),
            )
        )


def archive(s, target):
    if target == "file":
        DocumentService(s["engine"]).update_file(
            s["owner"],
            s["ledgers"][0],
            s["files"][0],
            FileUpdate(expected_version=1, archived=True),
        )
    else:
        with s["structure"]._transaction(s["owner"]) as session:
            _, entity = s["structure"]._ledger(session, s["owner"], s["ledgers"][0])
            entity_id, version = entity.id, entity.version
        s["structure"].update_entity(
            s["owner"], entity_id, EntityUpdate(expected_version=version, archived=True)
        )


def finances(engine):
    with engine.connect() as connection:
        return {
            model.__tablename__: connection.execute(
                select(model.__table__).order_by(*model.__table__.primary_key.columns)
            )
            .mappings()
            .all()
            for model in (FinancialOperation, Journal, JournalLine, CommandReceipt, OpeningPosition)
        }


def unchanged_failure(s, code, call):
    before = rows(s["engine"]), finances(s["engine"])
    with pytest.raises(LedgerError) as failure:
        call()
    assert failure.value.code == code
    assert (rows(s["engine"]), finances(s["engine"])) == before


def start_call(call, *, name):
    result = Future()

    def run():
        try:
            result.set_result(call())
        except BaseException as error:
            result.set_exception(error)

    thread = Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread, result


def join_calls(threads, release):
    release.set()
    for thread in threads:
        thread.join(timeout=10)
    assert not [thread.name for thread in threads if thread.is_alive()], (
        "Queue threads did not finish"
    )


def test_original_intent_survives_configuration_upgrade_and_archive(queue_structure):
    s = queue_structure
    created = create(s)
    old = rows(s["engine"])
    changed_configuration = s["configuration"] | {"processing": {"revision": 2}}
    assert create(s, intent=created.intent_id, configuration=changed_configuration) == created
    assert create(s, intent=created.intent_id, configuration={}) == created
    assert rows(s["engine"]) == old
    archive(s, "file")
    assert create(s, intent=created.intent_id).id == created.id
    assert s["queue"].list_jobs(s["owner"], s["ledgers"][0], intent_id=created.intent_id) == [
        s["queue"].get_job(s["owner"], s["ledgers"][0], created.id)
    ]
    unchanged_failure(
        s,
        "ocr_manifest_conflict",
        lambda: s["queue"].create_job(
            s["owner"],
            s["ledgers"][0],
            JobCreate(intent_id=created.intent_id, file_id=s["files"][1]),
            configuration=s["configuration"],
        ),
    )


@pytest.mark.parametrize("scope", ["owner", "ledger", "file"])
def test_queue_scope_errors_do_not_disclose_or_mutate_other_records(queue_structure, scope):
    s = queue_structure
    created = create(s)

    def call():
        if scope == "owner":
            return s["queue"].get_job(uuid4(), s["ledgers"][0], created.id)
        if scope == "ledger":
            return s["queue"].get_job(s["owner"], s["ledgers"][1], created.id)
        return s["queue"].create_job(
            s["owner"],
            s["ledgers"][0],
            JobCreate(intent_id=uuid4(), file_id=s["files"][1]),
            configuration=s["configuration"],
        )

    unchanged_failure(s, "not_found", call)
    if scope == "ledger":
        unchanged_failure(s, "not_found", lambda: create(s, ledger=1, intent=created.intent_id))


def test_concurrent_original_intent_creates_one_job_and_one_event(queue_structure):
    s = queue_structure
    intent, release = uuid4(), Event()
    ready = [Event(), Event()]
    threads, results = [], []

    def enqueue(index):
        ready[index].set()
        assert release.wait(timeout=5)
        return create(s, intent=intent)

    try:
        for index in range(2):
            thread, result = start_call(
                lambda index=index: enqueue(index), name=f"ocr-create-{index}"
            )
            threads.append(thread)
            results.append(result)
        assert all(item.wait(timeout=5) for item in ready)
        release.set()
        first, second = [item.result(timeout=5) for item in results]
        assert first == second
        records = rows(s["engine"])
        assert len(records["ocr_jobs"]) == 1
        assert [
            item["entity_version"]
            for item in records["change_log"]
            if item["entity_type"] == "ocr_jobs"
        ] == [1]
    finally:
        join_calls(threads, release)


def test_two_real_connections_claim_distinct_jobs_and_release_transaction_locks(queue_structure):
    s = queue_structure
    created = [create(s), create(s)]
    financial = finances(s["engine"])
    paused, release, second_ready = Event(), Event(), Event()
    threads, pids = [], {}

    def before_execute(connection, cursor, statement, parameters, context, many):
        name = current_thread().name
        if "pg_advisory_xact_lock" in statement and name in {"ocr-first", "ocr-second"}:
            pids[name] = connection.connection.driver_connection.info.backend_pid
            if name == "ocr-second":
                second_ready.set()

    def after_execute(connection, cursor, statement, parameters, context, many):
        if current_thread().name == "ocr-first" and "pg_advisory_xact_lock" in statement:
            paused.set()
            assert release.wait(timeout=5), "First claimant was not released"

    event.listen(s["queue_engine"], "before_cursor_execute", before_execute)
    event.listen(s["queue_engine"], "after_cursor_execute", after_execute)
    try:
        first, result_first = start_call(lambda: s["queue"].claim(s["owner"]), name="ocr-first")
        threads.append(first)
        assert paused.wait(timeout=5)
        second, result_second = start_call(lambda: s["queue"].claim(s["owner"]), name="ocr-second")
        threads.append(second)
        assert second_ready.wait(timeout=5)
        with s["engine"].connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
            deadline = monotonic() + 4
            while not observer.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid=:pid "
                    "AND wait_event_type='Lock' AND wait_event='advisory')"
                ),
                {"pid": pids["ocr-second"]},
            ):
                assert monotonic() < deadline, (
                    "Second claimant never reached the real database lock"
                )
                assert not release.wait(timeout=0.01)
        release.set()
        leases = [result_first.result(timeout=5), result_second.result(timeout=5)]
        assert len(set(pids.values())) == 2
        assert {item.job_id for item in leases} == {item.id for item in created}
        assert len({item.token for item in leases}) == 2
        assert s["queue"].claim(s["owner"]) is None
        with s["engine"].begin() as connection:
            assert connection.scalar(text("SELECT pg_try_advisory_xact_lock(18945999704708432)"))
            connection.execute(text("SELECT id FROM ledgers ORDER BY id FOR UPDATE NOWAIT"))
            connection.execute(text("SELECT id FROM entities ORDER BY id FOR UPDATE NOWAIT"))
            connection.execute(text("SELECT id FROM ocr_jobs ORDER BY id FOR UPDATE NOWAIT"))
        assert finances(s["engine"]) == financial
    finally:
        try:
            join_calls(threads, release)
        finally:
            event.remove(s["queue_engine"], "before_cursor_execute", before_execute)
            event.remove(s["queue_engine"], "after_cursor_execute", after_execute)


def test_database_clock_takeover_fences_all_old_worker_writes(queue_structure):
    s = queue_structure
    created = create(s)
    old = s["queue"].claim(s["owner"])
    expire(s, old)
    new = s["queue"].claim(s["owner"])
    assert new.job_id == old.job_id and new.token != old.token
    assert new.generation == old.generation + 1
    assert s["queue"].get_job(s["owner"], s["ledgers"][0], created.id).attempts == 2
    for call in (
        lambda: s["queue"].renew(old),
        lambda: s["queue"].fail(old, "processing_failed"),
        lambda: s["queue"].finish(old, completion()),
    ):
        unchanged_failure(s, "ocr_lease_lost", call)


def test_renewal_uses_live_token_generation_and_database_time(queue_structure):
    s = queue_structure
    create(s)
    lease = s["queue"].claim(s["owner"])
    renewed = s["queue"].renew(lease)
    assert renewed.token == lease.token and renewed.generation == lease.generation
    assert renewed.lease_until > lease.lease_until
    for forged in (
        replace(renewed, token=uuid4()),
        replace(renewed, generation=renewed.generation + 1),
    ):
        unchanged_failure(s, "ocr_lease_lost", lambda forged=forged: s["queue"].renew(forged))
    expire(s, renewed)
    unchanged_failure(s, "ocr_lease_lost", lambda: s["queue"].renew(renewed))


def test_retryable_failures_are_bounded_and_do_not_reset_counters(queue_structure):
    s = queue_structure
    created = create(s)
    for attempt in range(1, 4):
        lease = s["queue"].claim(s["owner"])
        failed = s["queue"].fail(lease, "processor_timeout", retryable=True)
        assert failed.attempts == attempt and failed.generation == attempt
        assert failed.state == ("pending" if attempt < 3 else "failed")
    assert s["queue"].claim(s["owner"]) is None
    unchanged_failure(
        s,
        "ocr_retry_exhausted",
        lambda: s["queue"].retry_job(s["owner"], s["ledgers"][0], created.id, failed.version),
    )


def test_expired_third_lease_terminates_instead_of_remaining_running(queue_structure):
    s = queue_structure
    created = create(s)
    for _ in range(3):
        lease = s["queue"].claim(s["owner"])
        expire(s, lease)
    assert s["queue"].claim(s["owner"]) is None
    failed = s["queue"].get_job(s["owner"], s["ledgers"][0], created.id)
    assert failed.state == "failed" and failed.attempts == 3
    assert failed.error_code is not None


def test_permanent_failure_is_not_automatically_requeued_and_manual_retry_uses_version(
    queue_structure,
):
    s = queue_structure
    created = create(s)
    lease = s["queue"].claim(s["owner"])
    failed = s["queue"].fail(lease, "invalid_document")
    assert failed.state == "failed" and s["queue"].claim(s["owner"]) is None
    unchanged_failure(
        s,
        "version_conflict",
        lambda: s["queue"].retry_job(s["owner"], s["ledgers"][0], created.id, failed.version - 1),
    )
    pending = s["queue"].retry_job(s["owner"], s["ledgers"][0], created.id, failed.version)
    assert pending.state == "pending" and pending.attempts == failed.attempts
    assert pending.generation == failed.generation and pending.config_hash == failed.config_hash
    unchanged_failure(
        s,
        "version_conflict",
        lambda: s["queue"].retry_job(s["owner"], s["ledgers"][0], created.id, failed.version),
    )


@pytest.mark.parametrize("target,operation", [("file", "finish"), ("entity", "renew")])
def test_archive_during_processing_terminates_without_new_candidates(
    queue_structure, target, operation
):
    s = queue_structure
    created = create(s)
    lease = s["queue"].claim(s["owner"])
    archive(s, target)
    finished = (
        s["queue"].finish(lease, completion()) if operation == "finish" else s["queue"].renew(lease)
    )
    assert finished.state == "failed" and finished.error_code == "source_archived"
    assert rows(s["engine"])["ocr_drafts"] == []
    assert create(s, intent=created.intent_id).id == created.id
    unchanged_failure(
        s, "file_archived" if target == "file" else "entity_archived", lambda: create(s)
    )


def test_claim_stops_already_archived_pending_source_without_computing(queue_structure):
    s = queue_structure
    created = create(s)
    archive(s, "file")
    assert s["queue"].claim(s["owner"]) is None
    stopped = s["queue"].get_job(s["owner"], s["ledgers"][0], created.id)
    assert stopped.state == "failed" and stopped.error_code == "source_archived"
    assert rows(s["engine"])["ocr_drafts"] == []


def test_successful_completion_replay_preserves_original_and_manually_edited_draft(queue_structure):
    s = queue_structure
    created = create(s)
    lease = s["queue"].claim(s["owner"])
    payload = completion()
    result = s["queue"].finish(lease, payload)
    assert result.state == "succeeded" and len(result.result.draft_ids) == 1
    assert result.result.summary == {"pages": 1, "manual_pages": 0, "candidates": 1}
    draft_id = result.result.draft_ids[0]
    with writer(s) as session:
        session.execute(
            update(OcrDraft)
            .where(OcrDraft.id == draft_id)
            .values(
                fields={"amount": "10.00", "fixture": "Fictional manual edit"},
                status="ignored",
                version=2,
                updated_at=func.clock_timestamp(),
            )
        )
    before = rows(s["engine"]), finances(s["engine"])
    assert s["queue"].finish(lease, payload) == result
    assert create(s, intent=created.intent_id) == result
    assert (rows(s["engine"]), finances(s["engine"])) == before
    unchanged_failure(
        s,
        "ocr_completion_conflict",
        lambda: s["queue"].finish(
            lease, Completion(summary={"changed": True}, candidates=payload.candidates)
        ),
    )
    for call in (
        lambda: s["queue"].renew(lease),
        lambda: s["queue"].fail(lease, "processing_failed"),
    ):
        unchanged_failure(s, "ocr_lease_lost", call)


def test_first_completion_keeps_preexisting_candidate_and_adds_only_new_source(queue_structure):
    s = queue_structure
    create(s)
    lease = s["queue"].claim(s["owner"])
    payload, draft_id = completion(second=True), uuid4()
    financial = finances(s["engine"])
    with writer(s) as session:
        session.execute(
            insert(OcrDraft).values(
                id=draft_id,
                ledger_id=lease.ledger_id,
                created_by=lease.owner_id,
                job_id=lease.job_id,
                **payload.candidates[0].model_dump(),
            )
        )
        session.execute(
            update(OcrDraft)
            .where(OcrDraft.id == draft_id)
            .values(
                fields={"amount": "10.00", "fixture": "Fictional retained manual edit"},
                status="ignored",
                version=2,
                updated_at=func.clock_timestamp(),
            )
        )
    original = rows(s["engine"])["ocr_drafts"][0]
    result = s["queue"].finish(lease, payload)
    records = rows(s["engine"])["ocr_drafts"]
    assert len(records) == 2 and result.result.draft_ids[0] == draft_id
    assert next(record for record in records if record["id"] == draft_id) == original
    assert {record["source_key"] for record in records} == {"page:1/line:1", "page:1/line:2"}
    committed = rows(s["engine"])
    assert s["queue"].finish(lease, payload) == result
    assert rows(s["engine"]) == committed and finances(s["engine"]) == financial


def test_failure_after_real_candidate_insert_rolls_back_state_drafts_and_logs(queue_structure):
    s = queue_structure
    create(s)
    lease = s["queue"].claim(s["owner"])
    before = rows(s["engine"]), finances(s["engine"])
    persisted = Event()

    def fail_after_insert(connection, cursor, statement, parameters, context, many):
        if "INSERT INTO ocr_drafts" in statement:
            persisted.set()
            raise RuntimeError("Fictional fault after candidate persistence")

    event.listen(s["queue_engine"], "after_cursor_execute", fail_after_insert)
    try:
        with pytest.raises(RuntimeError, match="Fictional fault"):
            s["queue"].finish(lease, completion(second=True))
    finally:
        event.remove(s["queue_engine"], "after_cursor_execute", fail_after_insert)
    assert persisted.is_set(), "The failure must follow a real INSERT, not only validation"
    assert (rows(s["engine"]), finances(s["engine"])) == before
    result = s["queue"].finish(lease, completion(second=True))
    assert len(result.result.draft_ids) == 2


def test_queue_changes_are_incremental_and_never_create_financial_records(queue_structure):
    s = queue_structure
    changes = ChangeService(s["engine"])
    before = changes.list_changes(s["owner"]).next_cursor
    financial = finances(s["engine"])
    created = create(s)
    lease = s["queue"].claim(s["owner"])
    result = s["queue"].finish(lease, completion())
    page = changes.list_changes(s["owner"], after=before)
    assert [item.entity_type for item in page.changes] == [
        "ocr_jobs",
        "ocr_jobs",
        "ocr_drafts",
        "ocr_jobs",
    ]
    assert [item.entity_version for item in page.changes] == [1, 2, 1, 3]
    assert all(
        item.ledger_id == created.ledger_id and item.owner_id == s["owner"] for item in page.changes
    )
    assert len(result.result.draft_ids) == 1
    assert page.changes[2].entity_id == str(result.result.draft_ids[0])
    assert page.changes[-1].entity_id == str(created.id)
    assert changes.list_changes(s["owner"], after=page.next_cursor).changes == []
    assert finances(s["engine"]) == financial
