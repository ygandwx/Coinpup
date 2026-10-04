"""Read committed change notifications without taking the business write lock."""

from pydantic import ValidationError
from sqlalchemy import select

from coinpup_api.ledger.service import LedgerError, LedgerService, _page
from coinpup_api.sync.models import ChangeLog
from coinpup_api.sync.schemas import ChangePage, ChangeRecord, cursor_value


class ChangeService(LedgerService):
    def list_changes(self, owner_id, *, after="0", limit=100) -> ChangePage:
        _page(limit, 0)
        try:
            cursor = cursor_value(after)
        except ValueError:
            raise LedgerError(
                "invalid_cursor", 422, "Use a bounded canonical decimal cursor."
            ) from None
        with self._transaction(owner_id, read_only=True) as session:
            # The owner check and transaction snapshot are established by LedgerService.
            records = session.scalars(
                select(ChangeLog)
                .where(ChangeLog.owner_id == owner_id, ChangeLog.seq > cursor)
                .order_by(ChangeLog.seq.asc())
                .limit(limit)
            ).all()
            try:
                changes = [
                    ChangeRecord(
                        seq=str(record.seq),
                        owner_id=record.owner_id,
                        ledger_id=record.ledger_id,
                        entity_type=record.entity_type,
                        entity_id=record.entity_id,
                        entity_version=record.entity_version,
                        change_kind=record.change_kind,
                        changed_at=record.changed_at,
                    )
                    for record in records
                ]
                return ChangePage(
                    changes=changes, next_cursor=changes[-1].seq if changes else after
                )
            except ValidationError:
                raise LedgerError(
                    "ledger_integrity", 503, "Stored change notifications are inconsistent."
                ) from None
