"""Dimension ownership, immutable reversal facts and legacy empty dimensions."""

import runpy
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.ledger.models import JournalLine
from coinpup_api.ledger.posting_schemas import CancellationCreate, CorrectionCreate
from coinpup_api.ledger.schemas import EntityCreate
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_business_references import seed
from tests.integration.ledger.test_exchange_constraints import _header, _legacy_principal
from tests.integration.ledger.test_posting_service_database import classified, opening
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ledger.test_revision_constraints import _revise

pytestmark = pytest.mark.integration
FIELDS = (
    "party_id",
    "counterparty_entity_id",
    "document_id",
    "document_line_id",
    "dimension_owner_id",
)


def dimension_set(s):
    party, document, line = seed(s)
    return dict(
        party_id=party,
        document_id=document,
        document_line_id=line,
        counterparty_entity_id=s["entity"].id,
        dimension_owner_id=s["owner"],
    )


def fixture(s):
    return s | {"accounts": [s["account"].id], "categories": [s["expenses"][0].id]}


def initial(s, dimensions):
    with s["engine"].begin() as connection:
        operation, journal = _header(connection, fixture(s), "expense")
        rows = [row | dimensions for row in _legacy_principal(fixture(s), journal, "expense")]
        connection.execute(insert(JournalLine), rows)
    return operation, journal


@pytest.mark.parametrize(
    "field",
    ["party_id", "document_id", "document_line_id", "counterparty_entity_id", "dimension_owner_id"],
)
def test_foreign_or_missing_dimension_rolls_back_every_table(ledger_setup, field):
    s = ledger_setup
    dimensions = dimension_set(s)
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(kind="personal", name="Fictional foreign dimensions", base_asset_id="USD"),
    )
    foreign = dimension_set(s | {"ledger": other.ledger.id, "entity": other})
    if field in ("counterparty_entity_id", "dimension_owner_id"):
        dimensions[field] = uuid4()
    else:
        dimensions[field] = foreign[field]
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        initial(s, dimensions)
    assert failure.value.orig.sqlstate == "23503"
    assert snapshot(s) == before


@pytest.mark.parametrize("field", ["document_id", "counterparty_entity_id", "dimension_owner_id"])
def test_partial_dimensions_are_rejected(ledger_setup, field):
    s = ledger_setup
    dimensions = dimension_set(s) | {field: None}
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        initial(s, dimensions)
    assert failure.value.orig.diag.constraint_name in {
        "ck_journal_lines_document_dimension",
        "ck_journal_lines_owner_dimension",
    }
    assert snapshot(s) == before


def test_document_line_must_belong_to_the_named_document_even_in_the_same_ledger(ledger_setup):
    s = ledger_setup
    dimensions = dimension_set(s)
    other = dimension_set(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        initial(s, dimensions | {"document_line_id": other["document_line_id"]})
    assert failure.value.orig.diag.constraint_name == "fk_journal_lines_document_line"
    assert snapshot(s) == before


@pytest.mark.parametrize("cancel", [False, True])
def test_existing_revision_service_copies_dimensions_and_preserves_original_receipts(
    ledger_setup, cancel
):
    s = ledger_setup
    dimensions = dimension_set(s)
    operation, journal = initial(s, dimensions)
    if cancel:
        payload = CancellationCreate(expected_version=1, reason="Fictional dimension cancellation")
        command = s["posting"].cancel_operation
    else:
        payload = CorrectionCreate(
            expected_version=1,
            reason="Fictional dimension correction",
            replacement=classified(s).model_dump(mode="json", exclude={"id"}) | {"kind": "expense"},
        )
        command = s["posting"].correct_operation
    receipt = command(s["owner"], s["ledger"], operation, payload, "fictional-dimensions")
    history = s["posting"].history(s["owner"], s["ledger"], operation)
    original = next(j for h in history for j in h.journals if j.id == journal)
    reverse = next(j for h in history for j in h.journals if j.kind == "reversal")
    for a, b in zip(original.lines, reverse.lines, strict=True):
        for field in FIELDS[:-1]:
            assert getattr(a, field) == getattr(b, field) == dimensions[field]
        assert a.amount == (b.amount[1:] if b.amount.startswith("-") else "-" + b.amount)
    before = snapshot(s)
    assert command(s["owner"], s["ledger"], operation, payload, "fictional-dimensions") == receipt
    assert snapshot(s) == before


@pytest.mark.parametrize("field", ["party_id", "document_line_id", "counterparty_entity_id"])
def test_deferred_reversal_rejects_changed_dimensions_without_replacing_old_validator(
    ledger_setup, field
):
    s = ledger_setup
    dimensions = dimension_set(s)
    operation, _ = initial(s, dimensions)
    before = snapshot(s)

    class CorruptReversal:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement, parameters=None):
            if getattr(getattr(statement, "table", None), "name", None) == "journal_lines":
                parameters = [dict(row) for row in parameters]
                parameters[0][field] = None
                if field == "counterparty_entity_id":
                    parameters[0]["dimension_owner_id"] = None
            return self.connection.execute(statement, parameters)

    with pytest.raises(IntegrityError) as failure:
        with s["engine"].begin() as connection:
            _revise(CorruptReversal(connection), fixture(s), operation, cancel=True)
    assert failure.value.orig.diag.constraint_name == "ck_journal_reversal_dimensions"
    assert snapshot(s) == before


def test_sealed_dimensions_cannot_be_changed_and_prevent_downgrade(ledger_setup):
    s = ledger_setup
    _, journal = initial(s, dimension_set(s))
    before = snapshot(s)
    with pytest.raises(IntegrityError):
        with s["engine"].begin() as connection:
            connection.execute(
                update(JournalLine).where(JournalLine.journal_id == journal).values(party_id=None)
            )
    migration = runpy.run_path(
        str(
            Path(__file__).resolve().parents[3]
            / "services/api/migrations/versions/20261009_0017_journal_dimensions.py"
        )
    )
    with pytest.raises(IntegrityError) as failure:
        with s["engine"].begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration["downgrade"]()
    assert failure.value.orig.diag.constraint_name == "ck_journal_dimensions_downgrade"
    assert snapshot(s) == before


def test_old_money_history_has_identical_json_and_survives_dimension_round_trip(ledger_setup):
    s = ledger_setup
    payload = opening(s)
    receipt = s["posting"].post_opening(
        s["owner"], s["ledger"], payload, "fictional-old-dimensions"
    )
    history = s["posting"].history(s["owner"], s["ledger"], receipt.id)
    for line in history[0].journals[0].lines:
        assert not set(FIELDS).intersection(line.model_dump(mode="json"))
    before = snapshot(s)
    migration = runpy.run_path(
        str(
            Path(__file__).resolve().parents[3]
            / "services/api/migrations/versions/20261009_0017_journal_dimensions.py"
        )
    )
    with s["engine"].begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration["downgrade"]()
            migration["upgrade"]()
        assert all(
            all(row[name] is None for name in FIELDS)
            for row in connection.execute(select(JournalLine.__table__)).mappings()
        )
    assert snapshot(s) == before
    assert (
        s["posting"].post_opening(s["owner"], s["ledger"], payload, "fictional-old-dimensions")
        == receipt
    )
    assert s["posting"].history(s["owner"], s["ledger"], receipt.id) == history
