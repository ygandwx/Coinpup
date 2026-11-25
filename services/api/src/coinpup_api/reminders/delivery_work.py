"""Shared ledger scope and clock refresh for the two delivery channels."""

from sqlalchemy import select

from coinpup_api.ledger.models import Ledger
from coinpup_api.ledger.service import _page, _touch
from coinpup_api.reminders.delivery_commands import store_clock
from coinpup_api.reminders.delivery_models import ReminderDeliveryConfig as Config
from coinpup_api.reminders.delivery_models import ReminderDeliveryConfigRevision as ConfigRevision
from coinpup_api.reminders.delivery_plan import project_delivery_plan
from coinpup_api.reminders.models import ReminderEventRevision as EventRevision
from coinpup_api.reminders.service import ReminderService


def get_delivery_config(session, ledger, event):
    return session.scalar(
        select(Config).where(Config.ledger_id == ledger, Config.event_id == event)
    )


def delivery_revisions(session, ledger, event, config):
    event_revision = session.scalar(
        select(EventRevision.id).where(
            EventRevision.ledger_id == ledger,
            EventRevision.event_id == event.id,
            EventRevision.version == event.version,
        )
    )
    config_revision = session.scalar(
        select(ConfigRevision.id).where(
            ConfigRevision.ledger_id == ledger,
            ConfigRevision.config_id == config.id,
            ConfigRevision.version == config.version,
        )
    )
    return event_revision, config_revision


class ReminderDeliveryWork(ReminderService):
    def owned_ledgers(self, owner, *, after=None, limit=50):
        _page(limit, 0)
        with self._transaction(owner, read_only=True) as session:
            statement = select(Ledger.id).where(Ledger.owner_id == owner)
            if after is not None:
                statement = statement.where(Ledger.id > after)
            return session.scalars(statement.order_by(Ledger.id).limit(limit)).all()

    def candidate_events(self, owner, ledger, *, after=None, limit=50):
        _page(limit, 0)
        with self._transaction(owner, read_only=True) as session:
            self._ledger(session, owner, ledger)
            statement = select(Config.event_id).where(Config.ledger_id == ledger)
            if after is not None:
                statement = statement.where(Config.event_id > after)
            return session.scalars(statement.order_by(Config.event_id).limit(limit)).all()

    def _write_scope(self, session, owner, ledger):
        # Archived entities can record historical acknowledgments, but cannot deliver anew.
        _, entity = self._ledger(session, owner, ledger)
        if not entity.archived:
            _, entity = self._ledger(session, owner, ledger, write=True)
        return entity

    @staticmethod
    def _plan(session, owner, event, config, entity):
        decision = project_delivery_plan(config, event, entity_archived=entity.archived)
        if decision.requires_update:
            store_clock(config, decision)
            config.last_actor_id, config.last_action = owner, "plan"
            _touch(config)
            session.flush()
        return decision
