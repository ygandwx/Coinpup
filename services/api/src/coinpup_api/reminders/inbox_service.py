"""Persist and read owned in-app notices without changing reminder completion."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, insert, select

from coinpup_api.ledger.service import LedgerError, _not_found, _page
from coinpup_api.reminders.delivery_work import (
    ReminderDeliveryWork,
    delivery_revisions,
    get_delivery_config,
)
from coinpup_api.reminders.inbox_models import ReminderInboxDelivery as Inbox


class ReminderInboxView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ledger_id: UUID
    event_id: UUID
    event_revision_id: UUID
    config_revision_id: UUID
    channel: Literal["inbox"]
    version: Literal[1, 2]
    planned_at: datetime
    delivered_at: datetime
    read_at: datetime | None
    title: str
    event_kind: Literal["annual", "tax", "certificate"]
    due_date: date
    date_basis: Literal["manual", "calculated"]
    locale: Literal["en", "zh"]


class ReminderInboxService(ReminderDeliveryWork):
    def deliver(self, owner, ledger, event_id) -> bool:
        with self._transaction(owner, write=True, read_committed=True) as session:
            entity = self._write_scope(session, owner, ledger)
            event = self._event(session, ledger, event_id)
            config = get_delivery_config(session, ledger, event_id)
            if config is None or not config.inbox_enabled:
                return False
            plan = self._plan(session, owner, event, config, entity)
            now = session.scalar(select(func.clock_timestamp()))
            if not plan.due(now):
                return False
            planned_at = plan.plan.scheduled_at
            if (
                session.scalar(
                    select(Inbox.id).where(
                        Inbox.ledger_id == ledger,
                        Inbox.event_id == event_id,
                        Inbox.planned_at == planned_at,
                    )
                )
                is not None
            ):
                return False
            event_revision, config_revision = delivery_revisions(session, ledger, event, config)
            if event_revision is None or config_revision is None:
                raise LedgerError("ledger_integrity", 503, "The inbox source is incomplete.")
            # The database verifies the current source and materializes its immutable content.
            session.execute(
                insert(Inbox).values(
                    id=uuid4(),
                    ledger_id=ledger,
                    event_id=event_id,
                    event_revision_id=event_revision,
                    config_revision_id=config_revision,
                    planned_at=planned_at,
                )
            )
            return True

    def list_deliveries(self, owner, ledger, *, unread_only=False, limit=50, offset=0):
        _page(limit, offset)
        with self._transaction(owner, read_only=True) as session:
            self._ledger(session, owner, ledger)
            statement = select(Inbox).where(Inbox.ledger_id == ledger)
            if unread_only:
                statement = statement.where(Inbox.read_at.is_(None))
            rows = session.scalars(
                statement.order_by(Inbox.planned_at.desc(), Inbox.id).limit(limit).offset(offset)
            )
            return [ReminderInboxView.model_validate(row) for row in rows]

    def mark_read(self, owner, ledger, notification):
        with self._transaction(owner, write=True, read_committed=True) as session:
            self._write_scope(session, owner, ledger)
            row = session.get(Inbox, notification)
            if row is None or row.ledger_id != ledger:
                raise _not_found()
            if row.read_at is None:
                row.version = 2
                session.flush()
                session.refresh(row, attribute_names=["read_at"])
            return ReminderInboxView.model_validate(row)
