"""Pure ADR 0038 email decisions for one permanent delivery ID, with caller-supplied time."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from coinpup_api.reminders.mail_attempt import MailDisposition, MailResult


class AttemptState(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"
    ACCEPTED = "accepted"
    VERIFIED_RECEIVED = "verified_received"
    STOPPED = "stopped"


class AttemptConflict(ValueError):
    """No update is allowed for the supplied state, generation, token or time."""


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("An explicit timezone is required.")
    return value.astimezone(UTC)


# Five bounded automatic retries at most. The original delivery ID stays fixed.
_BACKOFF = tuple(timedelta(minutes=n) for n in (1, 5, 15, 60, 240))


@dataclass(frozen=True)
class MailAttempt:
    delivery_id: UUID
    generation: int = 0
    state: AttemptState = AttemptState.PENDING
    lease_token: UUID | None = None
    lease_until: datetime | None = None
    retry_at: datetime | None = None
    retries_used: int = 0
    smtp_code: int | None = None
    resolution_reason: str | None = None

    def claim(self, now: datetime, token: UUID, lease_until: datetime):
        now, until = _utc(now), _utc(lease_until)
        if not isinstance(token, UUID):
            raise ValueError("An explicit UUID lease token is required.")
        if until <= now:
            raise ValueError("The lease must end after the claim.")
        if self.state not in (AttemptState.PENDING, AttemptState.RETRY_WAIT):
            raise AttemptConflict("This delivery cannot be claimed.")
        if self.retry_at is not None and now < self.retry_at:
            raise AttemptConflict("The delay has not elapsed.")
        return replace(
            self,
            generation=self.generation + 1,
            state=AttemptState.SENDING,
            lease_token=token,
            lease_until=until,
            retry_at=None,
            smtp_code=None,
        )

    def _finish(self, state, **changes):
        return replace(
            self, state=state, lease_token=None, lease_until=None, retry_at=None, **changes
        )

    def expire(self, now: datetime):
        now = _utc(now)
        if self.state != AttemptState.SENDING or self.lease_until is None or now < self.lease_until:
            raise AttemptConflict("No expired active lease.")
        return self._finish(AttemptState.UNKNOWN)

    def record(self, now: datetime, generation: int, token: UUID, result: MailResult):
        now = _utc(now)
        if (
            self.state != AttemptState.SENDING
            or type(generation) is not int
            or not isinstance(token, UUID)
            or generation != self.generation
            or token != self.lease_token
            or self.lease_until is None
            or now >= self.lease_until
        ):
            raise AttemptConflict("The attempt no longer owns an active lease.")
        disposition = result.disposition
        if disposition == MailDisposition.TEMPORARY_FAILURE and self.retries_used < len(_BACKOFF):
            wait = _BACKOFF[self.retries_used]
            return replace(
                self._finish(AttemptState.RETRY_WAIT, smtp_code=result.smtp_code),
                retries_used=self.retries_used + 1,
                retry_at=now + wait,
            )
        target = {
            MailDisposition.ACCEPTED: AttemptState.ACCEPTED,
            MailDisposition.UNKNOWN: AttemptState.UNKNOWN,
            MailDisposition.TEMPORARY_FAILURE: AttemptState.FAILED,
            MailDisposition.PERMANENT_FAILURE: AttemptState.FAILED,
        }[disposition]
        return self._finish(target, smtp_code=result.smtp_code)

    def cancel_unsent(self):
        if self.state not in (AttemptState.RETRY_WAIT, AttemptState.SENDING):
            raise AttemptConflict("This delivery is not waiting or claimed.")
        return self._finish(AttemptState.CANCELLED, smtp_code=None)

    def requeue_unsent(self, now: datetime):
        now = _utc(now)
        if (
            self.state != AttemptState.SENDING
            or self.lease_until is None
            or now >= self.lease_until
        ):
            raise AttemptConflict("Only a live pre-send attempt may be requeued.")
        return replace(self._finish(AttemptState.RETRY_WAIT, smtp_code=None), retry_at=now)

    def retry_explicitly(self, now: datetime):
        if self.state != AttemptState.FAILED:
            raise AttemptConflict("Only an explicitly failed delivery may be retried.")
        return replace(
            self._finish(AttemptState.RETRY_WAIT, retries_used=0, smtp_code=None),
            retry_at=_utc(now),
        )

    def resolve_unknown(self, state: AttemptState, reason: str):
        if self.state != AttemptState.UNKNOWN or state not in (
            AttemptState.VERIFIED_RECEIVED,
            AttemptState.STOPPED,
        ):
            raise AttemptConflict("Unknown delivery requires a terminal human resolution.")
        if not reason.strip() or len(reason.strip()) > 2000 or "\x00" in reason:
            raise ValueError("Specify a bounded reason for this resolution.")
        return self._finish(state, resolution_reason=reason.strip())
