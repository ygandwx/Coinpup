"""Document history, completion and tenant boundaries on disposable PostgreSQL."""

import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.documents.models import FileUpload, OperationFileLink, StoredFile
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.posting_schemas import OpeningCreate
from coinpup_api.ledger.schemas import AccountCreate, EntityCreate
from coinpup_api.ledger.service import LedgerService
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration


@pytest.fixture
def documents_structure(structure_database):
    engine, owner = structure_database
    structure, posting = LedgerService(engine), PostingService(engine)
    ledgers, operations = [], []
    for index in range(2):
        ledger = structure.create_entity(
            owner,
            EntityCreate(kind="personal", name=f"Fictional documents {index}", base_asset_id="USD"),
        ).ledger.id
        account = structure.create_account(
            owner, ledger, AccountCreate(name="Fictional wallet", kind="cash", asset_ids=["USD"])
        )
        receipt = posting.post_opening(
            owner,
            ledger,
            OpeningCreate(
                account_id=account.id,
                asset_id="USD",
                amount="100",
                transaction_date="2026-01-01",
            ),
            f"fictional-documents-{index}",
        )
        ledgers.append(ledger)
        operations.append(receipt.id)
    return {"engine": engine, "owner": owner, "ledgers": ledgers, "operations": operations}


def _file(fixture, ledger=0, **changes):
    values = {
        "id": uuid4(),
        "ledger_id": fixture["ledgers"][ledger],
        "created_by": fixture["owner"],
        "blob_key": uuid4().hex,
        "sha256": "a" * 64,
        "byte_size": 123,
        "detected_media_type": "application/pdf",
        "original_filename": "fictional-invoice.pdf",
        "title": "Fictional invoice",
    }
    return values | changes


def _upload(fixture, ledger=0, **changes):
    values = {
        "id": uuid4(),
        "ledger_id": fixture["ledgers"][ledger],
        "created_by": fixture["owner"],
        "original_filename": "fictional-invoice.pdf",
        "declared_size": 123,
        "operation_id": None,
        "manifest_hash": "b" * 64,
    }
    return values | changes


def _link(fixture, file_id, ledger=0, **changes):
    values = {
        "id": uuid4(),
        "file_id": file_id,
        "ledger_id": fixture["ledgers"][ledger],
        "created_by": fixture["owner"],
        "operation_id": fixture["operations"][ledger],
    }
    return values | changes


def _completion(file_id, /, **changes):
    return {
        "state": "ready",
        "file_id": file_id,
        "response": {"file_id": str(file_id), "original_title": "Fictional invoice"},
        "completed_at": func.clock_timestamp(),
    } | changes


def _assert_constraint(engine, statement, constraint):
    with pytest.raises(IntegrityError) as rejected:
        with engine.begin() as connection:
            connection.execute(statement)
    assert rejected.value.orig.diag.constraint_name == constraint


def test_completed_upload_preserves_receipt_after_metadata_archive(documents_structure):
    s = documents_structure
    file, upload = _file(s), _upload(s, operation_id=s["operations"][0])
    link = _link(s, file["id"])
    with s["engine"].begin() as connection:
        connection.execute(insert(StoredFile).values(file))
        connection.execute(insert(FileUpload).values(upload | {"response": None}))
        # Completion validation is deferred, so the link may be inserted afterwards.
        connection.execute(
            update(FileUpload).where(FileUpload.id == upload["id"]).values(_completion(file["id"]))
        )
        connection.execute(insert(OperationFileLink).values(link))
    with s["engine"].begin() as connection:
        original = connection.scalar(select(FileUpload.response))
        connection.execute(
            update(StoredFile)
            .where(StoredFile.id == file["id"])
            .values(
                title="Updated fictional invoice",
                archived=True,
                version=2,
                updated_at=func.clock_timestamp(),
            )
        )
        connection.execute(
            update(OperationFileLink)
            .where(OperationFileLink.id == link["id"])
            .values(archived=True, version=2, updated_at=func.clock_timestamp())
        )
    with s["engine"].begin() as connection:
        connection.execute(
            update(OperationFileLink)
            .where(OperationFileLink.id == link["id"])
            .values(archived=False, version=3, updated_at=func.clock_timestamp())
        )
        assert connection.scalar(select(FileUpload.response)) == original
        assert connection.scalar(select(FileUpload.state)) == "ready"
        assert connection.scalar(select(StoredFile.version)) == 2
        assert connection.scalar(select(OperationFileLink.version)) == 3


@pytest.mark.parametrize("table", [StoredFile, FileUpload, OperationFileLink])
def test_document_creator_must_own_ledger(documents_structure, table):
    s = documents_structure
    file = _file(s)
    with s["engine"].begin() as connection:
        connection.execute(insert(StoredFile).values(file))
    if table is StoredFile:
        values = _file(s, sha256="c" * 64)
    elif table is FileUpload:
        values = _upload(s)
    else:
        values = _link(s, file["id"])
    values["created_by"] = uuid4()
    _assert_constraint(
        s["engine"], insert(table).values(values), f"fk_{table.__tablename__}_ledger_owner"
    )


@pytest.mark.parametrize(
    "target", ["upload_operation", "upload_file", "link_operation", "link_file"]
)
def test_document_references_cannot_cross_ledgers(documents_structure, target):
    s = documents_structure
    first, second = _file(s), _file(s, ledger=1)
    upload = _upload(s)
    with s["engine"].begin() as connection:
        connection.execute(insert(StoredFile), [first, second])
        connection.execute(insert(FileUpload).values(upload))
    if target == "upload_operation":
        statement = insert(FileUpload).values(_upload(s, operation_id=s["operations"][1]))
        constraint = "fk_file_uploads_operation_ledger"
    elif target == "upload_file":
        statement = (
            update(FileUpload)
            .where(FileUpload.id == upload["id"])
            .values(_completion(second["id"]))
        )
        constraint = "fk_file_uploads_file_ledger"
    elif target == "link_operation":
        statement = insert(OperationFileLink).values(
            _link(s, first["id"], operation_id=s["operations"][1])
        )
        constraint = "fk_operation_file_links_operation_ledger"
    else:
        statement = insert(OperationFileLink).values(_link(s, second["id"]))
        constraint = "fk_operation_file_links_file_ledger"
    _assert_constraint(s["engine"], statement, constraint)


def test_deduplication_is_per_ledger_and_blob_keys_are_not_shared(documents_structure):
    s = documents_structure
    first = _file(s)
    with s["engine"].begin() as connection:
        connection.execute(insert(StoredFile).values(first))
        connection.execute(insert(StoredFile).values(_file(s, ledger=1)))
    _assert_constraint(
        s["engine"], insert(StoredFile).values(_file(s)), "uq_stored_files_ledger_content"
    )
    _assert_constraint(
        s["engine"],
        insert(StoredFile).values(_file(s, ledger=1, blob_key=first["blob_key"], sha256="c" * 64)),
        "uq_stored_files_blob_key",
    )


def test_file_checks_reject_invalid_storage_identity_and_metadata(documents_structure):
    s = documents_structure
    cases = [
        ({"blob_key": "../fictional.pdf"}, "ck_stored_files_blob_key"),
        ({"sha256": "x" * 64}, "ck_stored_files_sha256"),
        ({"byte_size": 0}, "ck_stored_files_byte_size"),
        ({"byte_size": -1}, "ck_stored_files_byte_size"),
        ({"detected_media_type": "image/svg+xml"}, "ck_stored_files_media_type"),
        ({"original_filename": " \t "}, "ck_stored_files_filename"),
        ({"original_filename": "bad\nname.pdf"}, "ck_stored_files_filename"),
        ({"title": " \t "}, "ck_stored_files_title"),
        ({"version": 2}, "ck_stored_files_immutable"),
        ({"archived": True}, "ck_stored_files_immutable"),
    ]
    for values, constraint in cases:
        _assert_constraint(s["engine"], insert(StoredFile).values(_file(s, **values)), constraint)


def test_file_identity_cannot_change_or_be_deleted(documents_structure):
    s = documents_structure
    file = _file(s)
    with s["engine"].begin() as connection:
        connection.execute(insert(StoredFile).values(file))
    changes = {
        "id": uuid4(),
        "ledger_id": s["ledgers"][1],
        "created_by": uuid4(),
        "blob_key": uuid4().hex,
        "sha256": "c" * 64,
        "byte_size": 456,
        "detected_media_type": "image/png",
        "original_filename": "changed.pdf",
        "created_at": datetime.now(UTC) + timedelta(days=1),
    }
    for field, value in changes.items():
        _assert_constraint(
            s["engine"],
            update(StoredFile)
            .where(StoredFile.id == file["id"])
            .values(**{field: value, "version": 2}),
            "ck_stored_files_immutable",
        )
    for version in (0, 1, 3):
        _assert_constraint(
            s["engine"],
            update(StoredFile)
            .where(StoredFile.id == file["id"])
            .values(title="Changed", version=version),
            "ck_stored_files_immutable",
        )
    _assert_constraint(s["engine"], delete(StoredFile), "ck_stored_files_immutable")


def test_links_keep_identity_versions_and_one_pair_even_after_archive(documents_structure):
    s = documents_structure
    file = _file(s)
    link = _link(s, file["id"])
    with s["engine"].begin() as connection:
        connection.execute(insert(StoredFile).values(file))
        connection.execute(insert(OperationFileLink).values(link))
        connection.execute(update(OperationFileLink).values(archived=True, version=2))
    _assert_constraint(
        s["engine"],
        insert(OperationFileLink).values(_link(s, file["id"])),
        "uq_operation_file_links_pair",
    )
    for field, value in {
        "id": uuid4(),
        "file_id": uuid4(),
        "operation_id": s["operations"][1],
        "ledger_id": s["ledgers"][1],
        "created_by": uuid4(),
        "created_at": datetime.now(UTC) + timedelta(days=1),
    }.items():
        _assert_constraint(
            s["engine"],
            update(OperationFileLink).values(**{field: value, "version": 3}),
            "ck_operation_file_links_immutable",
        )
    _assert_constraint(
        s["engine"],
        update(OperationFileLink).values(archived=False, version=2),
        "ck_operation_file_links_immutable",
    )
    _assert_constraint(s["engine"], delete(OperationFileLink), "ck_operation_file_links_immutable")


def test_pending_upload_manifest_and_ready_receipt_are_immutable(documents_structure):
    s = documents_structure
    file, upload = _file(s), _upload(s)
    with s["engine"].begin() as connection:
        connection.execute(insert(StoredFile).values(file))
        connection.execute(insert(FileUpload).values(upload))
    for field, value in {
        "id": uuid4(),
        "ledger_id": s["ledgers"][1],
        "created_by": uuid4(),
        "original_filename": "changed.pdf",
        "declared_size": 456,
        "operation_id": s["operations"][0],
        "manifest_hash": "c" * 64,
        "created_at": datetime.now(UTC) + timedelta(days=1),
    }.items():
        _assert_constraint(
            s["engine"],
            update(FileUpload).values(_completion(file["id"], **{field: value})),
            "ck_file_uploads_immutable",
        )
    _assert_constraint(s["engine"], delete(FileUpload), "ck_file_uploads_immutable")
    with s["engine"].begin() as connection:
        connection.execute(update(FileUpload).values(_completion(file["id"])))
    for values in (
        {"state": "pending", "file_id": None, "response": None, "completed_at": None},
        {"response": {"changed": True}},
        {"file_id": file["id"]},
        {"completed_at": func.clock_timestamp()},
    ):
        _assert_constraint(
            s["engine"], update(FileUpload).values(values), "ck_file_uploads_immutable"
        )
    _assert_constraint(s["engine"], delete(FileUpload), "ck_file_uploads_immutable")


def test_upload_completion_requires_all_fields_and_an_object_receipt(documents_structure):
    s = documents_structure
    file, upload = _file(s), _upload(s)
    with s["engine"].begin() as connection:
        connection.execute(insert(StoredFile).values(file))
        connection.execute(insert(FileUpload).values(upload))
    for omitted in ("file_id", "response", "completed_at"):
        _assert_constraint(
            s["engine"],
            update(FileUpload).values(_completion(file["id"], **{omitted: None})),
            "ck_file_uploads_completion",
        )
    _assert_constraint(
        s["engine"],
        update(FileUpload).values(_completion(file["id"], response=[])),
        "ck_file_uploads_response",
    )
    for values, constraint in (
        ({"declared_size": 0}, "ck_file_uploads_declared_size"),
        ({"manifest_hash": "invalid"}, "ck_file_uploads_manifest_hash"),
        ({"original_filename": "\n"}, "ck_file_uploads_filename"),
        ({"state": "ready"}, "ck_file_uploads_immutable"),
        ({"state": "pending", "response": {}}, "ck_file_uploads_completion"),
    ):
        _assert_constraint(s["engine"], insert(FileUpload).values(_upload(s, **values)), constraint)


def test_ready_size_validation_runs_at_commit_and_rolls_back_atomically(documents_structure):
    s = documents_structure
    file, upload = _file(s), _upload(s, declared_size=124)
    with s["engine"].begin() as connection:
        connection.execute(insert(StoredFile).values(file))
        connection.execute(insert(FileUpload).values(upload))
    with pytest.raises(IntegrityError) as rejected:
        with s["engine"].begin() as connection:
            connection.execute(update(FileUpload).values(_completion(file["id"])))
            assert connection.scalar(select(FileUpload.state)) == "ready"
    assert rejected.value.orig.diag.constraint_name == "ck_file_uploads_stored_content"
    with s["engine"].connect() as connection:
        assert connection.scalar(select(FileUpload.state)) == "pending"
        assert connection.scalar(select(FileUpload.response)) is None


def test_operation_upload_cannot_finish_without_its_link(documents_structure):
    s = documents_structure
    file, upload = _file(s), _upload(s, operation_id=s["operations"][0])
    with s["engine"].begin() as connection:
        connection.execute(insert(StoredFile).values(file))
        connection.execute(insert(FileUpload).values(upload))
    _assert_constraint(
        s["engine"],
        update(FileUpload).values(_completion(file["id"])),
        "ck_file_uploads_operation_link",
    )


@pytest.mark.parametrize("table", [StoredFile, FileUpload])
def test_downgrade_refuses_document_history(documents_structure, table):
    s = documents_structure
    with s["engine"].begin() as connection:
        connection.execute(insert(table).values(_file(s) if table is StoredFile else _upload(s)))
    migration = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261003_0008_documents.py"
        )
    )
    with pytest.raises(IntegrityError) as rejected:
        with s["engine"].begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration["downgrade"]()
    assert rejected.value.orig.diag.constraint_name == "ck_documents_downgrade"


def test_empty_documents_migration_round_trip(documents_structure):
    s = documents_structure
    migration = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261003_0008_documents.py"
        )
    )
    with s["engine"].begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration["downgrade"]()
            migration["upgrade"]()
        connection.execute(insert(StoredFile).values(_file(s)))
