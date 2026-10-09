"""Reminder commands reject ambiguous dates and client-supplied company scope."""

from datetime import datetime
from uuid import uuid4

import pytest
from coinpup_api.reminders.schemas import (
    ReminderCreate,
    ReminderManual,
    ReminderTransition,
    RuleSelection,
)
from pydantic import ValidationError


@pytest.mark.parametrize(
    "patch",
    [
        {"title": " "},
        {"title": "x\ny"},
        {"notes": "x\x00"},
        {"manual_due_date": "2026-02-01"},
        {"manual_reason": "Fictional reason"},
        {"manual_due_date": "2026-02-01", "manual_reason": " "},
        {"completed": True},
        {"evaluation_status": "calculated"},
        {"event_kind": "invoice"},
    ],
)
def test_invalid_create(patch):
    with pytest.raises(ValidationError):
        ReminderCreate.model_validate(
            dict(id=uuid4(), title="Fictional reminder", event_kind="tax") | patch
        )


@pytest.mark.parametrize(
    "value",
    ["2026-2-1", "2026-02-30", 1770000000, True, datetime(2026, 2, 1), "2026-02-01T00:00:00Z"],
)
def test_manual_dates_are_strict(value):
    with pytest.raises(ValidationError):
        ReminderManual(expected_version=1, manual_due_date=value, reason="Fictional correction")


@pytest.mark.parametrize(
    "patch",
    [
        {"country_code": "CN"},
        {"registration_date": "2020-01-01"},
        {"filing_year": True},
        {"filing_year": 0},
        {"filing_year": "2026"},
        {"applicability_confirmed": "true"},
        {"expiry_date": "2026/01/01"},
    ],
)
def test_scope_is_server_owned_and_parameters_are_strict(patch):
    with pytest.raises(ValidationError):
        RuleSelection.model_validate(
            dict(rule_id="certificate.expiry", rule_version="2026-10-10.1") | patch
        )


@pytest.mark.parametrize("value", [True, 0, "1", 1.2])
def test_transition_requires_exact_version(value):
    with pytest.raises(ValidationError):
        ReminderTransition(expected_version=value, action="complete")


def test_manual_only_and_explicit_clear_are_supported():
    row = ReminderCreate(
        id=uuid4(),
        event_kind="tax",
        title="  Fictional deadline  ",
        manual_due_date="2026-02-01",
        manual_reason="Fictional manual notice",
    )
    assert row.title == "Fictional deadline"
    assert row.rule is None
    clear = ReminderManual(expected_version=2, manual_due_date=None, reason="Fictional withdrawal")
    assert clear.manual_due_date is None
