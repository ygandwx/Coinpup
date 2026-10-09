"""Owner-scoped reminder history; explicit recalculation never changes manual overrides."""

from dataclasses import asdict

from pydantic_core import to_jsonable_python
from sqlalchemy import select

from coinpup_api.ledger.service import (
    LedgerError,
    LedgerService,
    _not_found,
    _page,
    _touch,
    _version,
)
from coinpup_api.reminders.catalog import RULES, get_rule
from coinpup_api.reminders.evaluation import ReminderEvaluationError, ReminderInputs, evaluate
from coinpup_api.reminders.models import ReminderEvent as Event
from coinpup_api.reminders.models import ReminderEventRevision as Revision
from coinpup_api.reminders.schemas import (
    ReminderResponse,
    ReminderRevisionResponse,
    ReminderRuleResponse,
)


class ReminderService(LedgerService):
    def list_rules(self, owner_id, ledger_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            return [ReminderRuleResponse.model_validate(rule) for rule in RULES]

    @staticmethod
    def _event(session, ledger_id, identifier):
        row = session.scalar(
            select(Event).where(Event.id == identifier, Event.ledger_id == ledger_id)
        )
        if row is None:
            raise _not_found()
        return row

    @staticmethod
    def _evaluate(entity, kind, selection):
        if selection is None:
            return dict(
                evaluation=None, evaluation_status="missing_parameters", calculated_date=None
            )
        try:
            rule = get_rule(selection.rule_id, selection.rule_version)
            expected_kind = "certificate" if rule.id == "certificate.expiry" else "annual"
            if kind != expected_kind:
                raise ReminderEvaluationError("reminder_rule_kind")
            inputs = ReminderInputs(
                entity_kind=entity.kind,
                country_code=entity.country_code,
                region_code=entity.region_code,
                company_type=entity.company_type,
                registration_date=entity.registration_date,
                **selection.model_dump(exclude={"rule_id", "rule_version"}),
            )
            result = evaluate(rule, inputs)
        except ReminderEvaluationError as error:
            raise LedgerError(
                error.code, 422, "Select a known rule version for this reminder kind."
            ) from None
        return dict(
            evaluation=to_jsonable_python(asdict(result)),
            evaluation_status=result.status,
            calculated_date=result.calculated_date,
        )

    @staticmethod
    def _response(row):
        data = {
            key: getattr(row, key)
            for key in ReminderResponse.model_fields
            if key != "effective_date"
        }
        effective = row.manual_due_date
        if effective is None and row.evaluation_status == "calculated":
            effective = row.calculated_date
        return ReminderResponse(**data, effective_date=effective)

    def create_event(self, owner_id, ledger_id, payload):
        with self._transaction(owner_id, write=True) as session:
            _, entity = self._ledger(session, owner_id, ledger_id, write=True)
            row = Event(
                ledger_id=ledger_id,
                last_actor_id=owner_id,
                last_action="create",
                **payload.model_dump(exclude={"rule"}),
                **self._evaluate(entity, payload.event_kind, payload.rule),
            )
            session.add(row)
            session.flush()
            return self._response(row)

    def _mutate(self, owner_id, ledger_id, identifier, payload, action, change):
        with self._transaction(owner_id, write=True) as session:
            _, entity = self._ledger(session, owner_id, ledger_id, write=True)
            row = self._event(session, ledger_id, identifier)
            _version(row, payload.expected_version)
            if row.archived and action != "restore":
                raise LedgerError("reminder_archived", 409, "Restore the reminder before editing.")
            values = change(row, entity)
            for key, value in values.items():
                setattr(row, key, value)
            row.last_action, row.last_actor_id = action, owner_id
            row.last_reason = getattr(payload, "reason", None)
            _touch(row)
            session.flush()
            session.refresh(row)
            return self._response(row)

    def edit_event(self, owner_id, ledger_id, identifier, payload):
        return self._mutate(
            owner_id,
            ledger_id,
            identifier,
            payload,
            "edit",
            lambda row, entity: dict(title=payload.title, notes=payload.notes),
        )

    def recalculate_event(self, owner_id, ledger_id, identifier, payload):
        return self._mutate(
            owner_id,
            ledger_id,
            identifier,
            payload,
            "recalculate",
            lambda row, entity: self._evaluate(entity, row.event_kind, payload.rule),
        )

    def set_manual_date(self, owner_id, ledger_id, identifier, payload):
        action = "set_manual" if payload.manual_due_date is not None else "clear_manual"
        return self._mutate(
            owner_id,
            ledger_id,
            identifier,
            payload,
            action,
            lambda row, entity: dict(
                manual_due_date=payload.manual_due_date,
                manual_reason=payload.reason if payload.manual_due_date else None,
            ),
        )

    def transition_event(self, owner_id, ledger_id, identifier, payload):
        field, value = {
            "complete": ("completed", True),
            "reopen": ("completed", False),
            "archive": ("archived", True),
            "restore": ("archived", False),
        }[payload.action]
        return self._mutate(
            owner_id,
            ledger_id,
            identifier,
            payload,
            payload.action,
            lambda row, entity: {field: value},
        )

    def get_event(self, owner_id, ledger_id, identifier):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            return self._response(self._event(session, ledger_id, identifier))

    def list_events(
        self,
        owner_id,
        ledger_id,
        *,
        include_archived=True,
        include_completed=True,
        limit=100,
        offset=0,
    ):
        _page(limit, offset)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            statement = select(Event).where(Event.ledger_id == ledger_id)
            if not include_archived:
                statement = statement.where(Event.archived.is_(False))
            if not include_completed:
                statement = statement.where(Event.completed.is_(False))
            return [
                self._response(row)
                for row in session.scalars(
                    statement.order_by(Event.created_at, Event.id).limit(limit).offset(offset)
                )
            ]

    def list_revisions(self, owner_id, ledger_id, identifier, *, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            self._event(session, ledger_id, identifier)
            statement = (
                select(Revision)
                .where(Revision.ledger_id == ledger_id, Revision.event_id == identifier)
                .order_by(Revision.version)
                .limit(limit)
                .offset(offset)
            )
            return [
                ReminderRevisionResponse.model_validate(row) for row in session.scalars(statement)
            ]
