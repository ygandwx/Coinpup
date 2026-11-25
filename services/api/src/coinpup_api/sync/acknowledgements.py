"""Acknowledge only a validated, persisted page; replay returns that page's original receipt."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from coinpup_api.ledger.service import LedgerError, _not_found
from coinpup_api.sync.ack_models import PageAcknowledgement as Ack
from coinpup_api.sync.device_models import DeviceRevision
from coinpup_api.sync.device_page import DevicePageReads
from coinpup_api.sync.devices import _query
from coinpup_api.sync.models import ChangeLog
from coinpup_api.sync.schemas import cursor_value


@dataclass(frozen=True, slots=True)
class AcknowledgedPage:
    owner_id: UUID
    device_id: UUID
    after: str
    through: str
    limit: int
    count: int
    cursor: str
    recorded_at: datetime


def receipt(row):
    return AcknowledgedPage(
        row.owner_id,
        row.device_id,
        str(row.after_seq),
        str(row.through_seq),
        row.page_limit,
        row.row_count,
        str(row.last_seq),
        row.recorded_at,
    )


class DeviceAcknowledgements(DevicePageReads):
    def latest(self, owner_id, device_id):
        with self._transaction(owner_id, read_only=True) as session:
            if session.scalar(_query(owner_id, device_id).limit(1)) is None:
                raise _not_found()
            row = session.scalar(
                select(Ack)
                .where(Ack.owner_id == owner_id, Ack.device_id == device_id)
                .order_by(Ack.last_seq.desc())
                .limit(1)
            )
            return None if row is None else receipt(row)

    def checkpoint(self, owner_id, device_id):
        with self._transaction(owner_id, read_only=True) as session:
            return self.checkpoint_for(session, owner_id, device_id)

    def checkpoint_for(self, session, owner_id, device_id, *, require_active=False):
        registry = session.scalar(
            _query(owner_id, device_id).order_by(DeviceRevision.version.desc()).limit(1)
        )
        if registry is None:
            raise _not_found()
        if require_active and registry.state != "registered":
            raise LedgerError("sync_device_retired", 409, "The device has been retired.")
        tail = session.scalar(
            select(Ack.last_seq)
            .where(Ack.owner_id == owner_id, Ack.device_id == device_id)
            .order_by(Ack.last_seq.desc())
            .limit(1)
        )
        position = registry.start_seq if tail is None else tail
        return self._checkpoint(session, owner_id, position)

    def acknowledge(self, owner_id, device_id, *, prior, after, through, limit, received):
        try:
            start, end = cursor_value(after), cursor_value(through)
        except ValueError:
            raise LedgerError("invalid_cursor", 422, "Invalid saved change window.") from None
        # The same write lock orders registry retirement, global notifications, and competing pages.
        with self._transaction(owner_id, write=True, read_committed=True) as session:
            registry = session.scalar(
                _query(owner_id, device_id).order_by(DeviceRevision.version.desc()).limit(1)
            )
            if registry is None:
                raise _not_found()
            scope = (Ack.owner_id == owner_id, Ack.device_id == device_id)
            old = session.scalar(select(Ack).where(*scope, Ack.after_seq == start))
            if old is not None and (old.page_limit != limit or old.through_seq != end):
                raise LedgerError(
                    "sync_checkpoint_conflict", 409, "The original page window differs."
                )
            if old is None and registry.state != "registered":
                raise LedgerError("sync_device_retired", 409, "The device has been retired.")
            self._require_anchor(session, owner_id, start, prior)
            tail = self._verify_in_snapshot(
                session, owner_id, after=after, through=through, limit=limit, received=received
            )
            if old is not None:
                if int(tail) != old.last_seq or len(received.changes) != old.row_count:
                    raise LedgerError("sync_page_conflict", 409, "The original receipt differs.")
                return receipt(old)
            if not received.changes:
                raise LedgerError("sync_empty_page", 409, "An empty page has no acknowledgement.")
            latest = session.scalar(
                select(Ack.last_seq).where(*scope).order_by(Ack.last_seq.desc()).limit(1)
            )
            checkpoint = registry.start_seq if latest is None else latest
            if checkpoint != start:
                raise LedgerError(
                    "sync_checkpoint_conflict", 409, "The device checkpoint has changed."
                )
            if (
                session.scalar(
                    select(ChangeLog.seq).where(
                        ChangeLog.owner_id == owner_id, ChangeLog.seq == end
                    )
                )
                is None
            ):
                raise LedgerError("sync_checkpoint_conflict", 409, "The saved upper bound differs.")
            row = Ack(
                owner_id=owner_id,
                device_id=device_id,
                after_seq=start,
                through_seq=end,
                prior_tail=latest,
                last_seq=int(tail),
                page_limit=limit,
                row_count=len(received.changes),
            )
            session.add(row)
            session.flush()
            return receipt(row)
