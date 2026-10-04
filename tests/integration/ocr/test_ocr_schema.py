"""Disposable PostgreSQL proves OCR storage guards without running an OCR engine."""

import runpy
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.files.models import StoredFile
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.ledger.service import LedgerService
from coinpup_api.ocr.models import OcrDraft, OcrJob
from coinpup_api.sync.locking import acquire_write_lock
from coinpup_api.sync.models import ChangeLog
from coinpup_api.sync.service import ChangeService
from sqlalchemy import delete, insert, null, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]


@contextmanager
def writer(s, ledger=0):
    with Session(s["engine"]) as session, session.begin():
        acquire_write_lock(session)
        s["structure"]._ledger(session, s["owner"], s["ledgers"][ledger], write=True)
        yield session


@pytest.fixture
def ocr_structure(structure_database):
    engine, owner = structure_database
    service = LedgerService(engine)
    ledgers = [
        service.create_entity(
            owner,
            EntityCreate(
                kind="personal", name=f"Fictional OCR ledger {index}", base_asset_id="USD"
            ),
        ).ledger.id
        for index in range(2)
    ]
    s = {"engine": engine, "owner": owner, "structure": service, "ledgers": ledgers, "files": []}
    for index in range(2):
        file = uuid4()
        with writer(s, index) as session:
            # Metadata-only fictional identities; no claim of a parsed or usable original.
            session.execute(
                insert(StoredFile).values(
                    id=file,
                    ledger_id=ledgers[index],
                    created_by=owner,
                    blob_key=uuid4().hex,
                    sha256="a" * 64,
                    byte_size=123,
                    detected_media_type="application/pdf",
                    original_filename="fictional-ocr.pdf",
                    title="Fictional OCR schema receipt",
                )
            )
        s["files"].append(file)
    return s


def job(s, ledger=0, **changes):
    return (
        dict(
            id=uuid4(),
            ledger_id=s["ledgers"][ledger],
            created_by=s["owner"],
            file_id=s["files"][ledger],
            intent_id=uuid4(),
            manifest_hash="b" * 64,
            config_hash="c" * 64,
            configuration={"fixture": "Fictional local OCR"},
        )
        | changes
    )


def draft(s, parent, **changes):
    return (
        dict(
            id=uuid4(),
            ledger_id=parent["ledger_id"],
            created_by=s["owner"],
            job_id=parent["id"],
            source_key="page:1/line:1",
            recognized={"amount": "12.30", "currency": "USD"},
            evidence={"page": 1, "text": "Fictional test receipt"},
            fields={},
        )
        | changes
    )


def add(s, model, values, ledger=0):
    with writer(s, ledger) as session:
        session.execute(insert(model).values(values))
    return values


def rows(engine):
    with engine.connect() as connection:
        return {
            model.__tablename__: connection.execute(
                select(model.__table__).order_by(
                    model.__table__.c.seq if model is ChangeLog else model.__table__.c.id
                )
            )
            .mappings()
            .all()
            for model in (OcrJob, OcrDraft, ChangeLog)
        }


def rejected(s, statement, sqlstate="23514", constraint=None, *, ledger=0):
    before = rows(s["engine"])
    with pytest.raises(IntegrityError) as error:
        with writer(s, ledger) as session:
            session.execute(statement)
    assert error.value.orig.sqlstate == sqlstate
    if constraint is not None:
        assert error.value.orig.diag.constraint_name == constraint
    elif sqlstate == "23514":
        assert error.value.orig.diag.constraint_name.startswith("ck_ocr_")
    assert rows(s["engine"]) == before


@pytest.mark.parametrize("scope", ["job_file", "job_owner", "draft_job", "draft_owner"])
def test_cross_ledger_and_owner_references_rejected(ocr_structure, scope):
    s = ocr_structure
    parent = add(s, OcrJob, job(s, ledger=1), ledger=1)
    if scope.startswith("job"):
        value = job(s, file_id=s["files"][1]) if scope == "job_file" else job(s, created_by=uuid4())
        statement = insert(OcrJob).values(value)
    else:
        value = draft(s, parent, ledger_id=s["ledgers"][0])
        if scope == "draft_owner":
            value = draft(s, parent, created_by=uuid4())
        statement = insert(OcrDraft).values(value)
    rejected(s, statement, "23503", ledger=1 if scope == "draft_owner" else 0)


def test_owner_intent_and_job_source_unique_across_retries(ocr_structure):
    s = ocr_structure
    parent = add(s, OcrJob, job(s))
    rejected(
        s,
        insert(OcrJob).values(job(s, ledger=1, intent_id=parent["intent_id"])),
        "23505",
        "uq_ocr_jobs_owner_intent",
        ledger=1,
    )
    add(s, OcrDraft, draft(s, parent))
    rejected(s, insert(OcrDraft).values(draft(s, parent)), "23505", "uq_ocr_drafts_job_source")


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "other"},
        {"state": None},
        {"attempts": -1},
        {"attempts": 4},
        {"generation": -1},
        {"lease_token": uuid4()},
        {"lease_until": datetime.now(UTC)},
        {"state": "running"},
        {"state": "succeeded"},
        {"state": "failed"},
        {"state": "succeeded", "result": {}, "error_code": "parse_failed"},
        {"result": []},
        {"result": {"text": "x" * 1048576}},
        {"error_code": "Unstable-Error"},
    ],
)
def test_invalid_job_state_lease_and_completion_shapes_rejected(ocr_structure, changes):
    s = ocr_structure
    parent = add(s, OcrJob, job(s))
    rejected(
        s,
        update(OcrJob).where(OcrJob.id == parent["id"]).values(version=2, **changes),
        "23502" if changes.get("state", "pending") is None else "23514",
    )


@pytest.mark.parametrize(
    "model,changes,sqlstate",
    [
        (OcrJob, {"configuration": []}, "23514"),
        (OcrJob, {"configuration": {"x": "x" * 16384}}, "23514"),
        (OcrJob, {"manifest_hash": "bad"}, "23514"),
        (OcrJob, {"config_hash": "g" * 64}, "23514"),
        (OcrJob, {"state": "failed", "error_code": "parse_failed"}, "23514"),
        (OcrDraft, {"recognized": []}, "23514"),
        (OcrDraft, {"evidence": {"text": "测" * 100000}}, "23514"),
        (OcrDraft, {"fields": None}, "23514"),
        (OcrDraft, {"fields": null()}, "23502"),
        (OcrDraft, {"source_key": ""}, "23514"),
        (OcrDraft, {"status": "ignored"}, "23514"),
    ],
)
def test_json_budgets_required_shapes_and_initial_states(ocr_structure, model, changes, sqlstate):
    s = ocr_structure
    parent = add(s, OcrJob, job(s))
    value = job(s) if model is OcrJob else draft(s, parent)
    rejected(s, insert(model).values(value | changes), sqlstate)


@pytest.mark.parametrize(
    "model,field,value",
    [
        (OcrJob, "id", uuid4()),
        (OcrJob, "intent_id", uuid4()),
        (OcrJob, "file_id", uuid4()),
        (OcrJob, "ledger_id", uuid4()),
        (OcrJob, "created_by", uuid4()),
        (OcrJob, "manifest_hash", "d" * 64),
        (OcrJob, "config_hash", "e" * 64),
        (OcrJob, "configuration", {}),
        (OcrDraft, "job_id", uuid4()),
        (OcrDraft, "source_key", "page:2/line:2"),
        (OcrDraft, "recognized", {}),
        (OcrDraft, "evidence", {}),
    ],
)
def test_source_identity_and_ocr_evidence_immutable(ocr_structure, model, field, value):
    s = ocr_structure
    parent = add(s, OcrJob, job(s))
    record = parent if model is OcrJob else add(s, OcrDraft, draft(s, parent))
    rejected(
        s,
        update(model).where(model.id == record["id"]).values(version=2, **{field: value}),
        constraint=f"ck_{model.__tablename__}_immutable",
    )


@pytest.mark.parametrize("model", [OcrJob, OcrDraft])
def test_delete_and_nonincrementing_version_rejected(ocr_structure, model):
    s = ocr_structure
    parent = add(s, OcrJob, job(s))
    record = parent if model is OcrJob else add(s, OcrDraft, draft(s, parent))
    rejected(s, delete(model).where(model.id == record["id"]))
    for version in (1, 3, None):
        rejected(s, update(model).where(model.id == record["id"]).values(version=version))


def test_mutable_fields_monotonic_counters_and_notifications_are_transactional(ocr_structure):
    s = ocr_structure
    parent, child = job(s), None
    before = rows(s["engine"])
    with writer(s) as session:
        session.execute(insert(OcrJob).values(parent))
        child = draft(s, parent)
        session.execute(insert(OcrDraft).values(child))
        assert (
            rows(s["engine"]) == before
        )  # Another connection cannot see uncommitted records/logs.
    cursor = str(before["change_log"][-1]["seq"])
    events = ChangeService(s["engine"]).list_changes(s["owner"], after=cursor).changes
    assert [(item.entity_type, item.entity_id, item.entity_version) for item in events] == [
        ("ocr_jobs", str(parent["id"]), 1),
        ("ocr_drafts", str(child["id"]), 1),
    ]
    assert all(item.owner_id == s["owner"] and item.ledger_id == s["ledgers"][0] for item in events)
    assert all(item.change_kind == "upsert" for item in events)
    with writer(s) as session:
        session.execute(
            update(OcrJob)
            .where(OcrJob.id == parent["id"])
            .values(
                version=2,
                state="running",
                attempts=1,
                generation=1,
                lease_token=uuid4(),
                lease_until=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        session.execute(
            update(OcrDraft)
            .where(OcrDraft.id == child["id"])
            .values(version=2, fields={"amount": "12.00"}, status="ignored")
        )
    updates = ChangeService(s["engine"]).list_changes(s["owner"], after=events[-1].seq).changes
    assert [(item.entity_type, item.entity_version) for item in updates] == [
        ("ocr_jobs", 2),
        ("ocr_drafts", 2),
    ]
    for field in ("attempts", "generation"):
        rejected(s, update(OcrJob).where(OcrJob.id == parent["id"]).values(version=3, **{field: 0}))
    committed = rows(s["engine"])
    with pytest.raises(RuntimeError, match="Fictional rollback"):
        with writer(s) as session:
            another = job(s)
            session.execute(insert(OcrJob).values(another))
            session.execute(insert(OcrDraft).values(draft(s, another)))
            raise RuntimeError("Fictional rollback")
    assert rows(s["engine"]) == committed


def test_successful_result_is_permanent_and_failed_job_can_retry(ocr_structure):
    s = ocr_structure
    parent = add(s, OcrJob, job(s))
    lease = dict(
        state="running",
        attempts=1,
        generation=1,
        lease_token=uuid4(),
        lease_until=datetime.now(UTC) + timedelta(hours=1),
    )
    with writer(s) as session:
        session.execute(update(OcrJob).where(OcrJob.id == parent["id"]).values(version=2, **lease))
    with writer(s) as session:
        session.execute(
            update(OcrJob)
            .where(OcrJob.id == parent["id"])
            .values(
                version=3,
                state="succeeded",
                lease_token=None,
                lease_until=None,
                result={"fixture": "Fictional preserved result"},
            )
        )
    for changes in ({}, {"result": {}}, {"state": "pending"}, {"attempts": 2, "generation": 2}):
        rejected(
            s,
            update(OcrJob).where(OcrJob.id == parent["id"]).values(version=4, **changes),
            constraint="ck_ocr_jobs_immutable",
        )
    another = add(s, OcrJob, job(s))
    with writer(s) as session:
        session.execute(
            update(OcrJob)
            .where(OcrJob.id == another["id"])
            .values(version=2, state="failed", attempts=1, generation=1, error_code="parse_failed")
        )
    with writer(s) as session:
        session.execute(
            update(OcrJob)
            .where(OcrJob.id == another["id"])
            .values(version=3, error_code=None, **(lease | {"attempts": 2, "generation": 2}))
        )


@pytest.mark.parametrize("history", ["data", "log", "empty"])
def test_scoped_migration_guards_data_logs_and_restores_empty_schema(ocr_structure, history):
    s = ocr_structure
    with s["engine"].connect() as connection:
        head = MigrationContext.configure(connection).get_current_heads()
    migration = runpy.run_path(
        str(ROOT / "services/api/migrations/versions/20261004_0013_ocr_jobs_and_drafts.py")
    )
    if history != "empty":
        parent = add(s, OcrJob, job(s))
        add(s, OcrDraft, draft(s, parent))
        with writer(s) as session:
            # Only this opted-in disposable fixture isolates each independent downgrade guard.
            if history == "data":
                session.execute(text("TRUNCATE TABLE change_log RESTRICT"))
            else:
                session.execute(text("TRUNCATE TABLE ocr_drafts, ocr_jobs RESTRICT"))
        before = rows(s["engine"])
        with pytest.raises(IntegrityError) as failure:
            with writer(s) as session:
                with Operations.context(MigrationContext.configure(session.connection())):
                    migration["downgrade"]()
        assert failure.value.orig.sqlstate == "23514"
        assert failure.value.orig.diag.constraint_name == "ck_ocr_downgrade"
        assert rows(s["engine"]) == before
    else:
        before = rows(s["engine"])
        with writer(s) as session:
            connection = session.connection()
            old = connection.execute(
                text("SELECT row_to_json(t)::text FROM stored_files t ORDER BY id")
            ).all()
            with Operations.context(MigrationContext.configure(connection)):
                migration["downgrade"]()
                migration["upgrade"]()
            assert (
                connection.execute(
                    text("SELECT row_to_json(t)::text FROM stored_files t ORDER BY id")
                ).all()
                == old
            )
        assert rows(s["engine"]) == before
    with s["engine"].connect() as connection:
        assert MigrationContext.configure(connection).get_current_heads() == head
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger "
                    "WHERE NOT tgisinternal AND tgname LIKE :pattern"
                ),
                {"pattern": "trg_%_change_log"},
            )
            == 11
        )
