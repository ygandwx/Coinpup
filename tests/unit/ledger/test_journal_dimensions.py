"""Dimensions cross the storage boundary exactly and never alter old JSON."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from coinpup_api.ledger.assets import get_asset
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.posting_storage import (
    DIMENSION_FIELDS,
    prepare_journal_lines,
    prepare_reversal_lines,
)

from tests.unit.ledger.test_posting_service import paired_lines


def test_storage_and_reversal_copy_every_dimension_without_reinterpreting_ids():
    dimensions = {name: uuid4() for name in DIMENSION_FIELDS}
    lines = [replace(line, **dimensions) for line in paired_lines()]
    catalog = {"USD": get_asset("USD")}
    original = prepare_journal_lines(lines, catalog)
    reversed_lines, parameters = prepare_reversal_lines(
        [SimpleNamespace(**row) for row in original], uuid4(), catalog
    )
    for source, reverse, line in zip(original, parameters, reversed_lines, strict=True):
        assert reverse["amount"] == source["amount"].copy_negate()
        assert all(
            source[name] == reverse[name] == getattr(line, name) == dimensions[name]
            for name in DIMENSION_FIELDS
        )


@pytest.mark.parametrize("field", DIMENSION_FIELDS)
def test_storage_rejects_non_uuid_dimensions(field):
    lines = paired_lines()
    lines[0] = replace(lines[0], **{field: str(uuid4())})
    with pytest.raises(MoneyError) as failure:
        prepare_journal_lines(lines, {"USD": get_asset("USD")})
    assert failure.value.code == "journal_dimension"


@pytest.mark.parametrize(
    "field", ["document_line_id", "counterparty_entity_id", "dimension_owner_id"]
)
def test_storage_rejects_incomplete_dimension_pairs(field):
    lines = paired_lines()
    lines[0] = replace(lines[0], **{field: uuid4()})
    with pytest.raises(MoneyError) as failure:
        prepare_journal_lines(lines, {"USD": get_asset("USD")})
    assert failure.value.code == "journal_dimension"
