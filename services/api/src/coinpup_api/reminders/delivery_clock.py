"""Pure reminder scheduling; no wall clock, database or sending."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from importlib.resources import files
from zoneinfo import ZoneInfo

import tzdata


class DeliveryClockError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DeliveryTime:
    scheduled_at: datetime
    local_date: date
    requested_minute: int
    resolved_minute: int
    timezone: str
    tzdata_version: str


@lru_cache(maxsize=256)
def _zone(key: str) -> ZoneInfo:
    # The locked first-party package is the same on Windows and Linux; ignore host TZPATH.
    if any(
        not part or any(not (char.isascii() and (char.isalnum() or char in "_+-")) for char in part)
        for part in key.split("/")
    ):
        raise DeliveryClockError("reminder_delivery_timezone")
    try:
        with files("tzdata.zoneinfo").joinpath(*key.split("/")).open("rb") as source:
            return ZoneInfo.from_file(source, key=key)
    except (OSError, ValueError):
        raise DeliveryClockError("reminder_delivery_timezone") from None


def schedule_delivery(due_date: date, lead_days: int, timezone: str, minute: int) -> DeliveryTime:
    """Resolve an explicit civil minute, preferring the first fold and staying on its date."""
    if type(due_date) is not date:
        raise DeliveryClockError("reminder_delivery_date")
    if type(lead_days) is not int or not 0 <= lead_days <= 3660:
        raise DeliveryClockError("reminder_delivery_lead_days")
    if type(minute) is not int or not 0 <= minute < 1440:
        raise DeliveryClockError("reminder_delivery_minute")
    if (
        type(timezone) is not str
        or not timezone
        or len(timezone) > 64
        or timezone in {"localtime", "posixrules"}
        or timezone.startswith(("posix/", "right/"))
    ):
        raise DeliveryClockError("reminder_delivery_timezone")
    zone = _zone(timezone)
    try:
        local_date = due_date - timedelta(days=lead_days)
        for resolved in range(minute, 1440):
            local = datetime.combine(local_date, time(resolved // 60, resolved % 60))
            candidates = set()
            for fold in (0, 1):
                utc = local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
                if utc.astimezone(zone).replace(tzinfo=None) == local:
                    candidates.add(utc)
            if candidates:
                return DeliveryTime(
                    min(candidates), local_date, minute, resolved, timezone, tzdata.__version__
                )
    except (OverflowError, ValueError):
        raise DeliveryClockError("reminder_delivery_date_range") from None
    raise DeliveryClockError("reminder_delivery_local_date_unavailable")
