"""One calendar occurrence, one transaction, one immutable draft identity."""

from datetime import datetime

from sqlalchemy import select, text

from coinpup_api.business.draft_schemas import BusinessDraftCreate
from coinpup_api.business.models import BusinessDocument, BusinessDocumentLine
from coinpup_api.business.models import RecurringInvoiceInstance as Instance
from coinpup_api.business.recurrence_calendar import (
    CalendarSchedule,
    RecurrenceError,
    instantiate_draft,
)
from coinpup_api.business.recurring_rules import RecurringRuleService
from coinpup_api.business.recurring_schemas import RecurringInstanceResponse
from coinpup_api.ledger.service import LedgerError, _not_found, _page, _touch


def aware_instant(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise LedgerError(
            "recurrence_instant", 422, "An explicit timezone-aware instant is required."
        )
    return now


def occurrence_index(index):
    if type(index) is not int or not 0 <= index < 2147483647:
        raise LedgerError(
            "recurrence_index", 422, "Occurrence index is outside the supported range."
        )
    return index


class RecurringGenerationService(RecurringRuleService):
    def generate_occurrence(self, owner_id, ledger_id, identifier, index, *, now):
        aware_instant(now)
        occurrence_index(index)
        with self._transaction(owner_id, write=True) as session:
            # Read ownership before replay; archival or changed references cannot invalidate it.
            self._ledger(session, owner_id, ledger_id)
            existing = session.scalar(
                select(Instance).where(
                    Instance.ledger_id == ledger_id,
                    Instance.rule_id == identifier,
                    Instance.occurrence_index == index,
                )
            )
            if existing is not None:
                return RecurringInstanceResponse.model_validate(existing)
            _, entity = self._ledger(session, owner_id, ledger_id, write=True)
            rule = self._rule(session, ledger_id, identifier)
            if rule.archived:
                raise LedgerError("recurrence_archived", 409, "The rule is paused.")
            if index != rule.next_index:
                raise LedgerError(
                    "recurrence_progress", 409, "Reload the next occurrence before retrying."
                )
            try:
                scheduled = CalendarSchedule(
                    rule.anchor_date, rule.frequency, rule.interval_count
                ).occurrence(index)
                body = instantiate_draft(
                    BusinessDraftCreate.model_validate(rule.template_input),
                    rule.id,
                    index,
                    scheduled,
                )
            except RecurrenceError as error:
                raise LedgerError(
                    error.code, 422, "The occurrence date cannot be represented."
                ) from None
            # PostgreSQL provides the same timezone database used to validate the saved rule.
            today = session.scalar(
                text(
                    "SELECT (CAST(:now AS timestamptz) AT TIME ZONE name)::date "
                    "FROM pg_catalog.pg_timezone_names WHERE name=:zone"
                ),
                {"now": now, "zone": rule.timezone_name},
            )
            if today is None:
                raise LedgerError("recurrence_timezone", 422, "The saved timezone is unavailable.")
            if scheduled > today:
                raise LedgerError("recurrence_not_due", 409, "The next occurrence is not due yet.")
            header, lines, _ = self._prepare(session, ledger_id, entity, body)
            session.add(BusinessDocument(id=body.id, **header))
            session.flush()
            session.add_all(BusinessDocumentLine(document_id=body.id, **line) for line in lines)
            session.flush()
            instance = Instance(
                id=body.id,
                ledger_id=ledger_id,
                rule_id=rule.id,
                occurrence_index=index,
                scheduled_date=scheduled,
                rule_version=rule.version,
                original_input=body.model_dump(mode="json"),
            )
            session.add(instance)
            session.flush()
            rule.next_index += 1
            _touch(rule)
            session.flush()
            return RecurringInstanceResponse.model_validate(instance)

    def get_instance(self, owner_id, ledger_id, identifier, index):
        occurrence_index(index)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            row = session.scalar(
                select(Instance).where(
                    Instance.ledger_id == ledger_id,
                    Instance.rule_id == identifier,
                    Instance.occurrence_index == index,
                )
            )
            if row is None:
                raise _not_found()
            return RecurringInstanceResponse.model_validate(row)

    def list_instances(self, owner_id, ledger_id, identifier, *, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            self._rule(session, ledger_id, identifier)
            return [
                RecurringInstanceResponse.model_validate(row)
                for row in session.scalars(
                    select(Instance)
                    .where(Instance.ledger_id == ledger_id, Instance.rule_id == identifier)
                    .order_by(Instance.occurrence_index)
                    .limit(limit)
                    .offset(offset)
                )
            ]
