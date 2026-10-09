"""Anchor-based dates and stable invoice draft identities; no posting or clock access."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal
from uuid import UUID, uuid5

from coinpup_api.business.draft_schemas import BusinessDraftCreate

Frequency = Literal["day", "week", "month", "year"]


class RecurrenceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _index(value):
    if type(value) is not int or not 0 <= value <= 2147483647:
        raise RecurrenceError("recurrence_index")
    return value


@dataclass(frozen=True)
class CalendarSchedule:
    anchor: date
    frequency: Frequency
    interval: int = 1

    def __post_init__(self):
        if type(self.anchor) is not date or self.frequency not in ("day", "week", "month", "year"):
            raise RecurrenceError("recurrence_schedule")
        if type(self.interval) is not int or not 1 <= self.interval <= 120:
            raise RecurrenceError("recurrence_interval")

    def occurrence(self, index: int) -> date:
        steps = self.interval * _index(index)
        try:
            if self.frequency in ("day", "week"):
                return self.anchor + timedelta(days=steps * (7 if self.frequency == "week" else 1))
            months = steps * (12 if self.frequency == "year" else 1)
            year, month = divmod(self.anchor.year * 12 + self.anchor.month - 1 + months, 12)
            if not 1 <= year <= 9999:
                raise RecurrenceError("recurrence_date_range")
            month += 1
            return date(year, month, min(self.anchor.day, monthrange(year, month)[1]))
        except (OverflowError, ValueError):
            raise RecurrenceError("recurrence_date_range") from None


def instance_id(rule_id: UUID, index: int) -> UUID:
    if not isinstance(rule_id, UUID):
        raise RecurrenceError("recurrence_rule_id")
    return uuid5(rule_id, f"invoice-occurrence:{_index(index)}")


def instantiate_draft(
    template: BusinessDraftCreate, rule_id: UUID, index: int, issue_date: date
) -> BusinessDraftCreate:
    """Copy raw prices and relative dates, never mutate the captured template."""
    if template.document_kind != "invoice" or type(issue_date) is not date:
        raise RecurrenceError("recurrence_template")
    identifier = instance_id(rule_id, index)
    offset = issue_date - template.issue_date
    payload = template.model_dump(mode="json")
    payload["id"] = str(identifier)
    payload["issue_date"] = issue_date.isoformat()
    try:
        if template.due_date is not None:
            payload["due_date"] = (template.due_date + offset).isoformat()
        for source, line in zip(template.lines, payload["lines"], strict=True):
            line["id"] = str(uuid5(identifier, f"line:{source.id}"))
            line["recognition_date"] = (source.recognition_date + offset).isoformat()
    except OverflowError:
        raise RecurrenceError("recurrence_date_range") from None
    return BusinessDraftCreate.model_validate(payload)
