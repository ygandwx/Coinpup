"""Fictional recurring dates and draft input preserve source intent without postings."""

from dataclasses import FrozenInstanceError
from datetime import date, datetime
from uuid import UUID, uuid4

import pytest
from coinpup_api.business.draft_schemas import BusinessDraftCreate
from coinpup_api.business.recurrence_calendar import (
    CalendarSchedule,
    RecurrenceError,
    instance_id,
    instantiate_draft,
)


@pytest.mark.parametrize(
    "anchor,frequency,interval,expected",
    [
        (date(2026, 1, 31), "month", 1, ["2026-01-31", "2026-02-28", "2026-03-31"]),
        (date(2024, 1, 31), "month", 1, ["2024-01-31", "2024-02-29", "2024-03-31"]),
        (date(2024, 2, 29), "year", 1, ["2024-02-29", "2025-02-28", "2026-02-28"]),
        (date(2026, 11, 30), "month", 3, ["2026-11-30", "2027-02-28", "2027-05-30"]),
        (date(2026, 12, 30), "day", 2, ["2026-12-30", "2027-01-01", "2027-01-03"]),
        (date(2026, 12, 28), "week", 2, ["2026-12-28", "2027-01-11", "2027-01-25"]),
    ],
)
def test_anchor_dates_do_not_drift(anchor, frequency, interval, expected):
    schedule = CalendarSchedule(anchor, frequency, interval)
    assert [schedule.occurrence(i).isoformat() for i in range(3)] == expected
    with pytest.raises(FrozenInstanceError):
        schedule.interval = 2


def test_leap_anchor_returns_to_original_day_and_centuries_obey_calendar():
    assert CalendarSchedule(date(2024, 2, 29), "year").occurrence(4) == date(2028, 2, 29)
    assert CalendarSchedule(date(2000, 2, 29), "year").occurrence(100) == date(2100, 2, 28)
    assert CalendarSchedule(date(2000, 2, 29), "year").occurrence(400) == date(2400, 2, 29)
    assert CalendarSchedule(date(1, 1, 1), "month").occurrence(0) == date.min


@pytest.mark.parametrize("interval", [True, 0, -1, 121, 1.0, "1", None])
def test_invalid_intervals(interval):
    with pytest.raises(RecurrenceError, match="recurrence_interval"):
        CalendarSchedule(date(2026, 1, 1), "month", interval)


@pytest.mark.parametrize(
    "anchor,frequency", [(datetime(2026, 1, 1), "day"), ("2026-01-01", "day"), (date.min, "hour")]
)
def test_explicit_calendar_inputs(anchor, frequency):
    with pytest.raises(RecurrenceError, match="recurrence_schedule"):
        CalendarSchedule(anchor, frequency)


@pytest.mark.parametrize("index", [True, -1, 2147483648, 1.0, "0", None])
def test_invalid_indices(index):
    with pytest.raises(RecurrenceError, match="recurrence_index"):
        CalendarSchedule(date(2026, 1, 1), "month").occurrence(index)
    with pytest.raises(RecurrenceError, match="recurrence_index"):
        instance_id(uuid4(), index)


@pytest.mark.parametrize("frequency", ["day", "week", "month", "year"])
def test_date_overflow_is_explicit(frequency):
    with pytest.raises(RecurrenceError, match="recurrence_date_range"):
        CalendarSchedule(date.max, frequency).occurrence(1)


def template():
    return BusinessDraftCreate(
        id=uuid4(),
        document_kind="invoice",
        party_id=uuid4(),
        asset_id="USD",
        issue_date="2026-01-31",
        due_date="2026-02-14",
        notes="Fictional monthly service",
        lines=[
            dict(
                id=uuid4(),
                description="Fictional service",
                quantity="2.00",
                unit_price="10.00",
                discount_amount="-0.00",
                tax_rate_percent="8.2500",
                category_id=uuid4(),
                project_id=uuid4(),
                recognition_date="2026-01-30",
            )
        ],
    )


def test_stable_scoped_identity_and_source_strings_with_relative_dates():
    source = template()
    source = source.model_copy(
        update={
            "lines": [
                source.lines[0],
                source.lines[0].model_copy(
                    update={"id": uuid4(), "recognition_date": date(2026, 2, 1)}
                ),
            ]
        }
    )
    original = source.model_dump(mode="json")
    rule = uuid4()
    result = instantiate_draft(source, rule, 1, date(2026, 2, 28))
    assert result == instantiate_draft(source, rule, 1, date(2026, 2, 28))
    assert source.model_dump(mode="json") == original
    assert result.id != source.id and result.lines[0].id != source.lines[0].id
    assert result.id == instance_id(rule, 1)
    assert result.id != instance_id(rule, 2) != instance_id(uuid4(), 2)
    assert result.issue_date == date(2026, 2, 28)
    assert result.due_date == date(2026, 3, 14)
    assert result.lines[0].recognition_date == date(2026, 2, 27)
    assert result.lines[1].recognition_date == date(2026, 3, 1)
    assert len({line.id for line in result.lines}) == 2
    later = instantiate_draft(source, rule, 2, date(2026, 3, 31))
    assert {line.id for line in result.lines}.isdisjoint(line.id for line in later.lines)
    assert result.lines[0].model_dump(exclude={"id", "recognition_date"}) == source.lines[
        0
    ].model_dump(exclude={"id", "recognition_date"})
    assert result.party_id == source.party_id and result.notes == source.notes
    empty = source.model_copy(update={"due_date": None, "lines": []})
    assert instantiate_draft(empty, rule, 2, date(2026, 3, 31)).lines == []


def test_invalid_kind_and_shifted_date_overflow_fail_without_mutating_source():
    source = template()
    with pytest.raises(RecurrenceError, match="recurrence_template"):
        instantiate_draft(source.model_copy(update={"document_kind": "bill"}), uuid4(), 0, date.min)
    with pytest.raises(RecurrenceError, match="recurrence_date_range"):
        instantiate_draft(source, uuid4(), 0, date.max)
    with pytest.raises(RecurrenceError, match="recurrence_rule_id"):
        instance_id("fictional", 0)
    assert source.due_date == date(2026, 2, 14)


@pytest.mark.parametrize("issue", ["2026-01-01", datetime(2026, 1, 1), None])
def test_instantiation_requires_a_calendar_date(issue):
    with pytest.raises(RecurrenceError, match="recurrence_template"):
        instantiate_draft(template(), uuid4(), 0, issue)


def test_recognition_date_underflow_rejects_the_whole_copy():
    source = template()
    source = source.model_copy(
        update={
            "due_date": None,
            "lines": [source.lines[0].model_copy(update={"recognition_date": date.min})],
        }
    )
    before = source.model_dump(mode="json")
    with pytest.raises(RecurrenceError, match="recurrence_date_range"):
        instantiate_draft(source, uuid4(), 0, date(2025, 1, 1))
    assert source.model_dump(mode="json") == before


def test_frozen_fictional_identity_protocol_survives_future_refactors():
    source = template()
    source = source.model_copy(
        update={
            "lines": [
                source.lines[0].model_copy(
                    update={"id": UUID("00000000-0000-4000-8000-000000000002")}
                )
            ]
        }
    )
    generated = instantiate_draft(
        source, UUID("00000000-0000-4000-8000-000000000001"), 0, date(2026, 1, 31)
    )
    assert str(generated.id) == "7e28e3c1-bc2e-5c10-8a6d-1d93e8a85c4f"
    assert str(generated.lines[0].id) == "ffec5453-94eb-500c-8c67-415ea51741a1"
