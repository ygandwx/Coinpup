"""Scheduling checks use fictional reminder dates, never legal deadlines."""

from datetime import UTC, date, datetime, timedelta

import pytest
import tzdata
from coinpup_api.reminders.delivery_clock import DeliveryClockError, schedule_delivery


@pytest.mark.parametrize(
    "due,lead,zone,minute,expected,resolved",
    [
        (date(2028, 1, 8), 7, "Asia/Shanghai", 540, datetime(2028, 1, 1, 1, tzinfo=UTC), 540),
        (date(2026, 3, 8), 0, "America/New_York", 150, datetime(2026, 3, 8, 7, tzinfo=UTC), 180),
        (
            date(2026, 11, 1),
            0,
            "America/New_York",
            90,
            datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            90,
        ),
        (
            date(2026, 10, 4),
            0,
            "Australia/Lord_Howe",
            135,
            datetime(2026, 10, 3, 15, 30, tzinfo=UTC),
            150,
        ),
        (date(2028, 3, 1), 1, "UTC", 0, datetime(2028, 2, 29, tzinfo=UTC), 0),
    ],
)
def test_fictional_civil_schedule_and_dst(due, lead, zone, minute, expected, resolved):
    result = schedule_delivery(due, lead, zone, minute)
    assert result.scheduled_at == expected
    assert result.resolved_minute == resolved
    assert result.requested_minute == minute
    assert result.timezone == zone
    assert result.local_date == due - timedelta(days=lead)
    assert result.tzdata_version == tzdata.__version__


@pytest.mark.parametrize(
    "args,code",
    [
        ((date(2011, 12, 30), 0, "Pacific/Apia", 540), "local_date_unavailable"),
        ((date.min, 1, "UTC", 0), "date_range"),
        ((date.min, 0, "Asia/Shanghai", 0), "date_range"),
        ((date.max, 0, "America/New_York", 1439), "date_range"),
        ((datetime(2026, 1, 1), 0, "UTC", 0), "date"),
        ((date(2026, 1, 1), True, "UTC", 0), "lead_days"),
        ((date(2026, 1, 1), -1, "UTC", 0), "lead_days"),
        ((date(2026, 1, 1), 3661, "UTC", 0), "lead_days"),
        ((date(2026, 1, 1), 0, "UTC", True), "minute"),
        ((date(2026, 1, 1), 0, "UTC", 1440), "minute"),
        ((date(2026, 1, 1), 0, "UTC", 0.5), "minute"),
        ((date(2026, 1, 1), 0, "Fictional/Missing", 0), "timezone"),
        ((date(2026, 1, 1), 0, "../UTC", 0), "timezone"),
        ((date(2026, 1, 1), 0, "localtime", 0), "timezone"),
        ((date(2026, 1, 1), 0, "right/UTC", 0), "timezone"),
        ((date(2026, 1, 1), 0, "posix/UTC", 0), "timezone"),
        ((date(2026, 1, 1), 0, "C:/UTC", 0), "timezone"),
        ((date(2026, 1, 1), 0, "/UTC", 0), "timezone"),
        ((date(2026, 1, 1), 0, "__init__.py", 0), "timezone"),
        ((date(2026, 1, 1), 0, "", 0), "timezone"),
    ],
)
def test_fictional_unavailable_date_or_invalid_configuration(args, code):
    with pytest.raises(DeliveryClockError) as caught:
        schedule_delivery(*args)
    assert caught.value.code == f"reminder_delivery_{code}"
