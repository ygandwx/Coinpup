"""Read committed change notifications without taking the business write lock."""

from pydantic import ValidationError
from sqlalchemy import select

from coinpup_api.ledger.service import LedgerError, LedgerService, _not_found, _page
from coinpup_api.sync.checkpoint import CheckpointConflict, matching_notice, verify_page
from coinpup_api.sync.models import ChangeLog
from coinpup_api.sync.schemas import ChangePage, ChangeRecord, ChangeStart, cursor_value


class ChangeService(LedgerService):
    def start_cursor(self, owner_id) -> ChangeStart:
        with self._transaction(owner_id, read_only=True) as session:
            # Identity allocation is not transactional. Anchor only to a delivered owner's
            # committed row, never to the PostgreSQL sequence, including after a rollback.
            latest = session.scalar(
                select(ChangeLog)
                .where(ChangeLog.owner_id == owner_id)
                .order_by(ChangeLog.seq.desc())
                .limit(1)
            )
            if latest is None:
                return ChangeStart(cursor="0", witness=None)
            return ChangeStart(cursor=str(latest.seq), witness=_record(latest))

    @staticmethod
    def _checkpoint(session, owner_id, position):
        if position == 0:
            return ChangeStart(cursor="0", witness=None)
        notice = session.scalar(
            select(ChangeLog).where(ChangeLog.owner_id == owner_id, ChangeLog.seq == position)
        )
        if notice is None:
            raise LedgerError("sync_checkpoint_conflict", 409, "The saved notification is missing.")
        return ChangeStart(cursor=str(position), witness=_record(notice))

    @classmethod
    def _require_anchor(cls, session, owner_id, position, prior):
        anchor = cls._checkpoint(session, owner_id, position)
        try:
            matches = prior is None if position == 0 else matching_notice(anchor.witness, prior)
        except CheckpointConflict:
            matches = False
        if not matches:
            raise LedgerError(
                "sync_checkpoint_conflict", 409, "The preceding notification differs."
            )
        return anchor

    @staticmethod
    def _page_in_snapshot(session, owner_id, cursor, end, limit, after):
        query = select(ChangeLog).where(ChangeLog.owner_id == owner_id, ChangeLog.seq > cursor)
        if end is not None:
            query = query.where(ChangeLog.seq <= end)
        records = session.scalars(query.order_by(ChangeLog.seq.asc()).limit(limit)).all()
        changes = [_record(record) for record in records]
        return ChangePage(changes=changes, next_cursor=changes[-1].seq if changes else after)

    def list_changes(self, owner_id, *, after="0", limit=100, through=None) -> ChangePage:
        _page(limit, 0)
        try:
            cursor = cursor_value(after)
            end = None if through is None else cursor_value(through)
        except ValueError:
            raise LedgerError(
                "invalid_cursor", 422, "Use a bounded canonical decimal cursor."
            ) from None
        if end is not None and end < cursor:
            raise LedgerError(
                "invalid_cursor", 422, "The upper cursor precedes the starting cursor."
            )
        with self._transaction(owner_id, read_only=True) as session:
            return self._page_in_snapshot(session, owner_id, cursor, end, limit, after)

    def verify_received_page(self, owner_id, *, after, through, limit, received):
        """Read-only check, never a durable device acknowledgement."""
        with self._transaction(owner_id, read_only=True) as session:
            return self._verify_in_snapshot(
                session, owner_id, after=after, through=through, limit=limit, received=received
            )

    def _verify_in_snapshot(self, session, owner_id, *, after, through, limit, received):
        _page(limit, 0)
        try:
            start, end = cursor_value(after), cursor_value(through)
            if end < start:
                raise ValueError
        except ValueError:
            raise LedgerError("invalid_cursor", 422, "Invalid saved change window.") from None
        actual = self._page_in_snapshot(session, owner_id, start, end, limit, after)
        try:
            return verify_page(
                owner_id,
                after=after,
                through=through,
                limit=limit,
                committed=actual,
                received=received,
            )
        except CheckpointConflict:
            raise LedgerError("sync_page_conflict", 409, "The committed page differs.") from None

    def change_at(self, owner_id, seq) -> ChangeRecord:
        """Read the exact notification saved by a device; never substitute a later row."""
        try:
            position = cursor_value(seq)
            if position == 0:
                raise ValueError
        except ValueError:
            raise LedgerError("invalid_cursor", 422, "Use a positive canonical sequence.") from None
        with self._transaction(owner_id, read_only=True) as session:
            record = session.scalar(
                select(ChangeLog).where(ChangeLog.owner_id == owner_id, ChangeLog.seq == position)
            )
            if record is None:
                raise _not_found()
            return _record(record)


def _record(record):
    try:
        return ChangeRecord(
            seq=str(record.seq),
            owner_id=record.owner_id,
            ledger_id=record.ledger_id,
            entity_type=record.entity_type,
            entity_id=record.entity_id,
            entity_version=record.entity_version,
            change_kind=record.change_kind,
            changed_at=record.changed_at,
        )
    except ValidationError:
        raise LedgerError(
            "ledger_integrity", 503, "Stored change notifications are inconsistent."
        ) from None
