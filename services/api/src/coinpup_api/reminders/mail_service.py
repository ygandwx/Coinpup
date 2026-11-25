"""Short, owner-scoped transactions around an otherwise independent SMTP attempt."""

from dataclasses import asdict, dataclass, field
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select

from coinpup_api.ledger.service import LedgerError, _not_found, _page, _touch, _version
from coinpup_api.reminders.delivery_commands import store_clock
from coinpup_api.reminders.delivery_models import ReminderDeliveryConfig as Config
from coinpup_api.reminders.delivery_models import ReminderDeliveryConfigRevision as ConfigRevision
from coinpup_api.reminders.delivery_plan import project_delivery_plan
from coinpup_api.reminders.mail_attempt import ReminderMail
from coinpup_api.reminders.mail_lifecycle import AttemptConflict, AttemptState, MailAttempt
from coinpup_api.reminders.mail_models import ReminderEmailDelivery as Delivery
from coinpup_api.reminders.mail_models import ReminderEmailEvidence as Evidence
from coinpup_api.reminders.mail_schemas import ReminderEmailView
from coinpup_api.reminders.models import ReminderEventRevision as EventRevision
from coinpup_api.reminders.service import ReminderService

LEASE = timedelta(seconds=90)


@dataclass(frozen=True)
class EmailClaim:
    token: UUID
    message: ReminderMail = field(repr=False)


def _config(session, ledger, event):
    return session.scalar(
        select(Config).where(Config.ledger_id == ledger, Config.event_id == event)
    )


def _latest(delivery_id):
    return (
        select(Evidence)
        .where(Evidence.delivery_id == delivery_id)
        .order_by(Evidence.version.desc())
    )


def _events(ledger, event):
    latest = (
        select(func.max(Evidence.version))
        .where(Evidence.delivery_id == Delivery.id)
        .correlate(Delivery)
        .scalar_subquery()
    )
    return (
        select(Delivery, Evidence)
        .join(Evidence, (Evidence.delivery_id == Delivery.id) & (Evidence.version == latest))
        .where(Delivery.ledger_id == ledger, Delivery.event_id == event)
    )


def _attempt(delivery, fact=None):
    if fact is None:
        return MailAttempt(delivery.id)
    return MailAttempt(
        delivery.id,
        generation=fact.attempt_no,
        state=AttemptState(fact.state),
        lease_token=fact.id if fact.state == AttemptState.SENDING else None,
        lease_until=fact.lease_until,
        retry_at=fact.retry_at,
        retries_used=fact.retries_used,
        smtp_code=fact.smtp_code,
        resolution_reason=fact.reason,
    )


def _append(session, delivery, prior, attempt, now, **claim_evidence):
    fact = Evidence(
        id=attempt.lease_token or uuid4(),
        delivery_id=delivery.id,
        ledger_id=delivery.ledger_id,
        version=1 if prior is None else prior.version + 1,
        attempt_no=attempt.generation,
        state=attempt.state.value,
        recorded_at=now,
        retries_used=attempt.retries_used,
        lease_until=attempt.lease_until,
        retry_at=attempt.retry_at,
        smtp_code=attempt.smtp_code,
        reason=attempt.resolution_reason,
        **claim_evidence,
    )
    session.add(fact)
    session.flush()
    return fact


def _view(delivery, fact):
    return ReminderEmailView(
        id=delivery.id,
        ledger_id=delivery.ledger_id,
        event_id=delivery.event_id,
        channel=delivery.channel,
        planned_at=delivery.planned_at,
        version=fact.version,
        attempt_no=fact.attempt_no,
        state=fact.state,
        recorded_at=fact.recorded_at,
        retry_at=fact.retry_at,
        smtp_code=fact.smtp_code,
    )


def _message(event, config, delivery, sender, due):
    # The due date and title identify the event without including notes or attachments.
    label = "到期日" if config.locale == "zh" else "Due date"
    title = " ".join(event.title.split())
    title = "".join(char for char in title if ord(char) >= 32 and ord(char) != 127)
    if not title:
        title = "事项提醒" if config.locale == "zh" else "Reminder"
    return ReminderMail(
        delivery.id,
        sender,
        config.email_recipient,
        title,
        f"{title}\n{label} {due.isoformat()}",
    )


def _open_lease(fact, token, now):
    return fact.state == AttemptState.SENDING and fact.id == token and now < fact.lease_until


class ReminderEmailService(ReminderService):
    def candidate_events(self, owner, ledger, *, after=None, limit=50):
        with self._transaction(owner, read_only=True) as session:
            self._ledger(session, owner, ledger)
            statement = select(Config.event_id).where(Config.ledger_id == ledger)
            if after is not None:
                statement = statement.where(Config.event_id > after)
            return session.scalars(statement.order_by(Config.event_id).limit(limit)).all()

    def list_deliveries(self, owner, ledger, event, *, limit=50, offset=0):
        _page(limit, offset)
        with self._transaction(owner, read_only=True) as session:
            self._ledger(session, owner, ledger)
            self._event(session, ledger, event)
            statement = _events(ledger, event).order_by(Delivery.planned_at.desc(), Delivery.id)
            return [_view(*row) for row in session.execute(statement.limit(limit).offset(offset))]

    def _write_scope(self, session, owner, ledger):
        # Check archive state while holding the change lock. An archived entity may
        # finish historical evidence, but only active entities may claim new mail.
        _, entity = self._ledger(session, owner, ledger)
        if not entity.archived:
            _, entity = self._ledger(session, owner, ledger, write=True)
        return entity

    @staticmethod
    def _delivery(session, ledger, identifier, *, event=None):
        row = session.get(Delivery, identifier)
        if row is None or row.ledger_id != ledger or (event is not None and row.event_id != event):
            raise _not_found()
        return row, session.scalar(_latest(identifier).limit(1))

    @staticmethod
    def _plan(session, owner, event, config, entity):
        decision = project_delivery_plan(config, event, entity_archived=entity.archived)
        if decision.requires_update:
            store_clock(config, decision)
            config.last_actor_id, config.last_action = owner, "plan"
            _touch(config)
            session.flush()
        return decision

    @staticmethod
    def _reconcile(session, ledger, event, planned_at, now):
        rows = session.execute(
            _events(ledger, event).where(
                Evidence.state.in_((AttemptState.RETRY_WAIT, AttemptState.SENDING))
            )
        ).all()
        for delivery, fact in rows:
            attempt = _attempt(delivery, fact)
            if fact.state == AttemptState.SENDING and fact.lease_until <= now:
                _append(session, delivery, fact, attempt.expire(now), now)
            elif fact.state == AttemptState.RETRY_WAIT and delivery.planned_at != planned_at:
                _append(session, delivery, fact, attempt.cancel_unsent(), now)

    def claim(self, owner, ledger, identifier, sender):
        with self._transaction(owner, write=True, read_committed=True) as session:
            entity = self._write_scope(session, owner, ledger)
            event = self._event(session, ledger, identifier)
            config = _config(session, ledger, identifier)
            if config is None:
                return None
            decision = self._plan(session, owner, event, config, entity)
            now = session.scalar(select(func.clock_timestamp()))
            planned_at = (
                decision.plan.scheduled_at if config.email_enabled and decision.due(now) else None
            )
            self._reconcile(session, ledger, identifier, planned_at, now)
            if planned_at is None:
                return None

            delivery = session.scalar(
                select(Delivery).where(
                    Delivery.event_id == identifier,
                    Delivery.ledger_id == ledger,
                    Delivery.channel == "email",
                    Delivery.planned_at == planned_at,
                )
            )
            prior = None
            if delivery is None:
                delivery = Delivery(
                    id=uuid4(),
                    event_id=identifier,
                    ledger_id=ledger,
                    channel="email",
                    planned_at=planned_at,
                )
                session.add(delivery)
                session.flush()
            else:
                prior = session.scalar(_latest(delivery.id).limit(1))
            try:
                attempt = _attempt(delivery, prior).claim(now, uuid4(), now + LEASE)
            except AttemptConflict:
                return None

            envelope = _message(event, config, delivery, sender, decision.plan.inputs.due)
            envelope.message()
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
            snapshot = asdict(envelope)
            snapshot.pop("delivery_id")
            fact = _append(
                session,
                delivery,
                prior,
                attempt,
                now,
                event_revision_id=event_revision,
                config_revision_id=config_revision,
                envelope=snapshot,
            )
            return EmailClaim(fact.id, ReminderMail(delivery.id, **snapshot))

    def is_live(self, owner, ledger, claim):
        with self._transaction(owner, read_only=True) as session:
            _, entity = self._ledger(session, owner, ledger)
            delivery, fact = self._delivery(session, ledger, claim.message.delivery_id)
            now = session.scalar(select(func.clock_timestamp()))
            if entity.archived or not _open_lease(fact, claim.token, now):
                return False
            event = self._event(session, ledger, delivery.event_id)
            config = _config(session, ledger, event.id)
            event_revision = session.get(EventRevision, fact.event_revision_id)
            config_revision = session.get(ConfigRevision, fact.config_revision_id)
            return (
                config is not None
                and event.version == event_revision.version
                and config.version == config_revision.version
            )

    def finish(self, owner, ledger, claim, smtp_result):
        with self._transaction(owner, write=True, read_committed=True) as session:
            entity = self._write_scope(session, owner, ledger)
            delivery, prior = self._delivery(session, ledger, claim.message.delivery_id)
            if prior.id != claim.token or prior.state != AttemptState.SENDING:
                return _view(delivery, prior)
            now = session.scalar(select(func.clock_timestamp()))
            attempt = _attempt(delivery, prior)
            if not _open_lease(prior, claim.token, now):
                result = attempt.expire(now)
            elif smtp_result is None:
                event = self._event(session, ledger, delivery.event_id)
                config = _config(session, ledger, event.id)
                result = attempt.cancel_unsent()
                if config is not None and config.email_enabled:
                    decision = self._plan(session, owner, event, config, entity)
                    if decision.due(now) and decision.plan.scheduled_at == delivery.planned_at:
                        result = attempt.requeue_unsent(now)
            else:
                result = attempt.record(now, prior.attempt_no, claim.token, smtp_result)
            return _view(delivery, _append(session, delivery, prior, result, now))

    def decide(self, owner, ledger, event_id, delivery_id, payload):
        with self._transaction(owner, write=True, read_committed=True) as session:
            entity = self._write_scope(session, owner, ledger)
            event = self._event(session, ledger, event_id)
            delivery, prior = self._delivery(session, ledger, delivery_id, event=event_id)
            _version(prior, payload.expected_version)
            now = session.scalar(select(func.clock_timestamp()))
            attempt = _attempt(delivery, prior)
            try:
                if payload.action == "retry":
                    config = _config(session, ledger, event_id)
                    self._authorize_retry(config, event, entity, delivery, now)
                    result = attempt.retry_explicitly(now)
                else:
                    target = (
                        AttemptState.VERIFIED_RECEIVED
                        if payload.action == "verify_received"
                        else AttemptState.STOPPED
                    )
                    result = attempt.resolve_unknown(target, payload.reason)
            except AttemptConflict:
                raise LedgerError(
                    "reminder_email_state", 409, "The email cannot change to that state."
                ) from None
            return _view(delivery, _append(session, delivery, prior, result, now))

    @staticmethod
    def _authorize_retry(config, event, entity, delivery, now):
        if config is not None and config.email_enabled:
            decision = project_delivery_plan(config, event, entity_archived=entity.archived)
            if decision.due(now) and decision.plan.scheduled_at == delivery.planned_at:
                return
        raise LedgerError("reminder_email_not_eligible", 409, "The email is no longer scheduled.")
