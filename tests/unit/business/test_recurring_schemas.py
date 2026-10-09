"""Calendar fields and captured templates cannot be silently rewritten by clients."""

from uuid import uuid4

import pytest
from coinpup_api.business.recurring_schemas import (
    RecurringRuleArchive,
    RecurringRuleCreate,
    RecurringRuleUpdate,
)
from pydantic import ValidationError


def values():
    return dict(
        id=uuid4(),
        name="Fictional 月末",
        timezone_name="Asia/Shanghai",
        anchor_date="2026-01-31",
        frequency="month",
        interval_count=1,
        source_document_id=uuid4(),
        source_version=1,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"frequency": "hour"},
        {"interval_count": 0},
        {"interval_count": 121},
        {"interval_count": True},
        {"interval_count": "1"},
        {"source_version": False},
        {"source_version": 0},
        {"anchor_date": "2026-01-31T00:00:00Z"},
        {"anchor_date": 20260131},
        {"timezone_name": " Asia/Shanghai"},
        {"timezone_name": "UTC\x00"},
        {"timezone_name": ""},
        {"name": " \t"},
        {"ledger_id": uuid4()},
        {"next_index": 1},
        {"template_input": {}},
        {"archived": True},
    ],
)
def test_create_rejects_invalid_and_server_owned_input(changes):
    with pytest.raises(ValidationError):
        RecurringRuleCreate(**(values() | changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"source_document_id": uuid4()},
        {"source_version": 2},
        {"anchor_date": "2026-01-31"},
        {"frequency": "year"},
        {"interval_count": 3},
        {"timezone_name": "UTC"},
        {"next_index": 1},
        {"archived": True},
    ],
)
def test_update_rejects_partial_capture_or_calendar_and_progress_changes(changes):
    with pytest.raises(ValidationError):
        RecurringRuleUpdate(expected_version=1, name="Fictional", **changes)


def test_rename_refresh_and_archive_have_distinct_intents():
    rename = RecurringRuleUpdate(expected_version=1, name=" Fictional 月末 ")
    assert rename.name == "Fictional 月末" and rename.source_document_id is None
    refresh = RecurringRuleUpdate(
        expected_version=1, name=rename.name, source_document_id=uuid4(), source_version=3
    )
    assert refresh.source_version == 3
    assert RecurringRuleCreate(**values()).interval_count == 1
    assert RecurringRuleArchive(expected_version=1, archived=True).archived is True
    with pytest.raises(ValidationError):
        RecurringRuleArchive(expected_version=1, archived="true")
