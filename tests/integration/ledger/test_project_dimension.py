"""Project references are sealed facts, scoped by ledger and copied on reversal."""

import runpy
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.models import BusinessProject
from coinpup_api.ledger.models import JournalLine
from coinpup_api.ledger.posting_schemas import CancellationCreate, CorrectionCreate
from coinpup_api.ledger.schemas import EntityCreate
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from tests.integration.conftest import project_dimension_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_journal_dimensions import fixture, initial
from tests.integration.ledger.test_posting_service_database import classified, opening
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ledger.test_projects import seed
from tests.integration.ledger.test_revision_constraints import _revise

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("foreign", [False, True])
def test_missing_or_cross_ledger_project_rolls_back_all_tables(ledger_setup, foreign):
    s = ledger_setup
    identifier = uuid4()
    if foreign:
        entity = s["structure"].create_entity(
            s["owner"],
            EntityCreate(kind="personal", name="Fictional project owner", base_asset_id="USD"),
        )
        identifier = seed(s, ledger_id=entity.ledger.id)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        initial(s, {"project_id": identifier})
    assert failure.value.orig.diag.constraint_name == "fk_journal_lines_project_ledger"
    assert snapshot(s) == before


@pytest.mark.parametrize("cancel", [False, True])
def test_project_history_and_reversal_survive_archive_and_permanent_replay(ledger_setup, cancel):
    s = ledger_setup
    project = seed(s)
    operation, original_id = initial(s, {"project_id": project})
    with s["engine"].begin() as c:
        c.execute(
            update(BusinessProject)
            .where(BusinessProject.id == project)
            .values(archived=True, version=2)
        )
    if cancel:
        payload = CancellationCreate(expected_version=1, reason="Fictional project cancellation")
        command = s["posting"].cancel_operation
    else:
        payload = CorrectionCreate(
            expected_version=1,
            reason="Fictional project correction",
            replacement=classified(s).model_dump(mode="json", exclude={"id"}) | {"kind": "expense"},
        )
        command = s["posting"].correct_operation
    receipt = command(s["owner"], s["ledger"], operation, payload, "fictional-project-history")
    history = s["posting"].history(s["owner"], s["ledger"], operation)
    original = next(j for h in history for j in h.journals if j.id == original_id)
    reverse = next(j for h in history for j in h.journals if j.kind == "reversal")
    for a, b in zip(original.lines, reverse.lines, strict=True):
        assert a.project_id == b.project_id == project
        assert a.model_dump(mode="json")["project_id"] == str(project)
        assert a.amount == (b.amount[1:] if b.amount.startswith("-") else "-" + b.amount)
    before = snapshot(s)
    assert (
        command(s["owner"], s["ledger"], operation, payload, "fictional-project-history") == receipt
    )
    assert snapshot(s) == before


@pytest.mark.parametrize("replacement", ["missing", "different"])
def test_deferred_reversal_rejects_project_omission_or_substitution(ledger_setup, replacement):
    s = ledger_setup
    original = seed(s)
    other = seed(s) if replacement == "different" else None
    operation, _ = initial(s, {"project_id": original})
    before = snapshot(s)

    class CorruptReversal:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement, parameters=None):
            if getattr(getattr(statement, "table", None), "name", None) == "journal_lines":
                parameters = [dict(row) for row in parameters]
                parameters[0]["project_id"] = other
            return self.connection.execute(statement, parameters)

    with pytest.raises(IntegrityError) as failure:
        with s["engine"].begin() as c:
            _revise(CorruptReversal(c), fixture(s), operation, cancel=True)
    assert failure.value.orig.diag.constraint_name == "ck_journal_reversal_dimensions"
    assert snapshot(s) == before


def test_sealed_project_reference_blocks_update_and_destructive_downgrade(ledger_setup):
    s = ledger_setup
    project = seed(s)
    _, journal = initial(s, {"project_id": project})
    before = snapshot(s)
    with pytest.raises(IntegrityError):
        with s["engine"].begin() as c:
            c.execute(
                update(JournalLine).where(JournalLine.journal_id == journal).values(project_id=None)
            )
    assert snapshot(s) == before
    with pytest.raises(IntegrityError) as failure:
        with s["engine"].begin() as c:
            with Operations.context(MigrationContext.configure(c)):
                project_dimension_migration()["downgrade"]()
    assert failure.value.orig.diag.constraint_name == "ck_project_dimensions_downgrade"
    assert snapshot(s) == before


def test_empty_dimension_round_trip_preserves_legacy_json_and_receipts(ledger_setup):
    s = ledger_setup
    payload = opening(s)
    receipt = s["posting"].post_opening(
        s["owner"], s["ledger"], payload, "fictional-project-legacy"
    )
    history = s["posting"].history(s["owner"], s["ledger"], receipt.id)
    json_before = [item.model_dump_json() for item in history]
    assert all(
        "project_id" not in line.model_dump()
        for item in history
        for journal in item.journals
        for line in journal.lines
    )
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[3]
            / "services/api/migrations/versions/20261009_0017_journal_dimensions.py"
        )
    )
    assert project_dimension_migration()["_OLD_REVERSAL_SQL"] == frozen["_REVERSAL_SQL"].replace(
        "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION"
    )
    before = snapshot(s)
    with s["engine"].begin() as c:
        with Operations.context(MigrationContext.configure(c)):
            project_dimension_migration()["downgrade"]()
            project_dimension_migration()["upgrade"]()
        assert all(value is None for value in c.scalars(select(JournalLine.project_id)))
    assert snapshot(s) == before
    assert [
        item.model_dump_json() for item in s["posting"].history(s["owner"], s["ledger"], receipt.id)
    ] == json_before
    assert (
        s["posting"].post_opening(s["owner"], s["ledger"], payload, "fictional-project-legacy")
        == receipt
    )
    assert snapshot(s) == before
