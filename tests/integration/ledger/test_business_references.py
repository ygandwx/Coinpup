"""Minimal reference identities retain scope and history without business commands."""

import runpy
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.models import BusinessDocument, BusinessDocumentLine, BusinessParty
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.sync.models import ChangeLog
from coinpup_api.sync.service import ChangeService
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.conftest import (
    draft_header_migration,
    draft_line_migration,
    party_profile_migration,
    period_migration,
    project_dimension_migration,
    project_migration,
)
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration
MODELS = (BusinessParty, BusinessDocument, BusinessDocumentLine)


def seed(s):
    ids = [uuid4() for _ in MODELS]
    with s["engine"].begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(18945999704708432)"))
        for model, identifier in zip(MODELS, ids, strict=True):
            values = dict(id=identifier, ledger_id=s["ledger"])
            if model is BusinessDocumentLine:
                values["document_id"] = ids[1]
            connection.execute(insert(model).values(values))
    return ids


def test_reference_changes_are_scoped_versioned_and_transactional(ledger_setup):
    s = ledger_setup
    ids = seed(s)
    records = ChangeService(s["engine"]).list_changes(s["owner"], limit=200).changes
    relevant = [r for r in records if r.entity_type.startswith("business_")]
    assert [(r.entity_type, r.entity_id, r.entity_version, r.change_kind) for r in relevant] == [
        (model.__tablename__, str(identifier), 1, "upsert")
        for model, identifier in zip(MODELS, ids, strict=True)
    ]
    assert all(r.owner_id == s["owner"] and r.ledger_id == s["ledger"] for r in relevant)
    for version, archived, kind in ((2, True, "archive"), (3, False, "restore")):
        with s["engine"].begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(18945999704708432)"))
            for model, identifier in zip(MODELS, ids, strict=True):
                connection.execute(
                    update(model)
                    .where(model.id == identifier)
                    .values(version=version, archived=archived)
                )
        records = (
            ChangeService(s["engine"])
            .list_changes(s["owner"], after=records[-1].seq, limit=200)
            .changes
        )
        assert len(records) == 3
        assert all(r.change_kind == kind and r.entity_version == version for r in records)
    before = snapshot(s)
    with pytest.raises(RuntimeError, match="Fictional rollback"):
        with s["engine"].begin() as connection:
            connection.execute(insert(BusinessParty).values(id=uuid4(), ledger_id=s["ledger"]))
            raise RuntimeError("Fictional rollback")
    assert snapshot(s) == before


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("mutation", ["delete", "id", "ledger", "version", "insert_version"])
def test_reference_identity_cannot_be_deleted_moved_or_silently_rewritten(
    ledger_setup, model, mutation
):
    s = ledger_setup
    ids = seed(s)
    identifier = ids[MODELS.index(model)]
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        with s["engine"].begin() as connection:
            if mutation == "delete":
                statement = delete(model).where(model.id == identifier)
            elif mutation == "insert_version":
                values = dict(id=uuid4(), ledger_id=s["ledger"], version=2)
                if model is BusinessDocumentLine:
                    values["document_id"] = ids[1]
                statement = insert(model).values(values)
            else:
                values = {"version": 2}
                values.update(
                    {
                        "id": {"id": uuid4()},
                        "ledger": {"ledger_id": uuid4()},
                        "version": {"version": 1, "archived": True},
                    }[mutation]
                )
                statement = update(model).where(model.id == identifier).values(values)
            connection.execute(statement)
    assert failure.value.orig.diag.constraint_name == "ck_business_reference_identity"
    assert snapshot(s) == before


def test_document_line_rejects_cross_ledger_and_reparenting(ledger_setup):
    s = ledger_setup
    ids = seed(s)
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(kind="personal", name="Fictional other references", base_asset_id="USD"),
    )
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        with s["engine"].begin() as connection:
            connection.execute(
                insert(BusinessDocumentLine).values(
                    id=uuid4(), ledger_id=other.ledger.id, document_id=ids[1]
                )
            )
    assert failure.value.orig.diag.constraint_name == "fk_business_document_lines_document_ledger"
    with pytest.raises(IntegrityError) as failure:
        with s["engine"].begin() as connection:
            connection.execute(
                update(BusinessDocumentLine)
                .where(BusinessDocumentLine.id == ids[2])
                .values(document_id=uuid4(), version=2)
            )
    assert failure.value.orig.diag.constraint_name == "ck_business_reference_identity"
    assert snapshot(s) == before


@pytest.mark.parametrize("history", ["empty", "rows", "logs"])
def test_reference_migration_preserves_history_and_empty_round_trip(ledger_setup, history):
    s = ledger_setup
    migration = runpy.run_path(
        str(
            Path(__file__).resolve().parents[3]
            / "services/api/migrations/versions/20261009_0016_business_references.py"
        )
    )
    if history != "empty":
        seed(s)
        with s["engine"].begin() as connection:
            if history == "rows":
                connection.exec_driver_sql("TRUNCATE TABLE change_log RESTRICT")
            else:
                assert (
                    connection.exec_driver_sql("SELECT count(*) FROM journal_lines").scalar() == 0
                )
                connection.exec_driver_sql(
                    "TRUNCATE TABLE journal_lines, business_document_lines, business_documents, "
                    "business_parties RESTRICT"
                )
        before = snapshot(s)
        with pytest.raises(IntegrityError) as failure:
            with s["engine"].begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    migration["downgrade"]()
        assert failure.value.orig.diag.constraint_name == "ck_business_reference_downgrade"
        assert snapshot(s) == before
    else:
        before = snapshot(s)
        dimensions = runpy.run_path(
            str(
                Path(__file__).resolve().parents[3]
                / "services/api/migrations/versions/20261009_0017_journal_dimensions.py"
            )
        )
        with s["engine"].begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                draft_line_migration()["downgrade"]()
                draft_header_migration()["downgrade"]()
                project_dimension_migration()["downgrade"]()
                project_migration()["downgrade"]()
                party_profile_migration()["downgrade"]()
                period_migration()["downgrade"]()
                dimensions["downgrade"]()
                migration["downgrade"]()
                migration["upgrade"]()
                dimensions["upgrade"]()
                period_migration()["upgrade"]()
                party_profile_migration()["upgrade"]()
                project_migration()["upgrade"]()
                project_dimension_migration()["upgrade"]()
                draft_header_migration()["upgrade"]()
                draft_line_migration()["upgrade"]()
        assert snapshot(s) == before
        seed(s)
        with s["engine"].connect() as connection:
            assert (
                len(
                    connection.scalars(
                        select(ChangeLog).where(
                            ChangeLog.entity_type.in_([m.__tablename__ for m in MODELS])
                        )
                    ).all()
                )
                == 3
            )
