"""An authenticated owner's device history. A device ID alone never authorizes a call."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from coinpup_api.ledger.service import LedgerError, _not_found
from coinpup_api.sync.device_models import DeviceRevision
from coinpup_api.sync.schemas import ChangeStart, cursor_value
from coinpup_api.sync.service import ChangeService


@dataclass(frozen=True, slots=True)
class DeviceState:
    owner_id: UUID
    device_id: UUID
    version: int
    state: str
    recorded_at: datetime
    start: ChangeStart


def _query(owner_id, device_id):
    if not isinstance(device_id, UUID):
        raise LedgerError("invalid_device", 422, "Provide a stable device UUID.")
    return select(DeviceRevision).where(
        DeviceRevision.owner_id == owner_id, DeviceRevision.device_id == device_id
    )


class DeviceRegistry(ChangeService):
    def _view(self, session, row):
        anchor = self._checkpoint(session, row.owner_id, row.start_seq)
        return DeviceState(
            row.owner_id, row.device_id, row.version, row.state, row.recorded_at, anchor
        )

    def history(self, owner_id, device_id):
        query = _query(owner_id, device_id)
        with self._transaction(owner_id, read_only=True) as session:
            rows = session.scalars(query.order_by(DeviceRevision.version)).all()
            if not rows:
                raise _not_found()
            return tuple(self._view(session, row) for row in rows)

    def register(self, owner_id, device_id, start=None):
        return self._change(owner_id, device_id, retire=False, start=start)

    def retire(self, owner_id, device_id):
        return self._change(owner_id, device_id, retire=True)

    def _change(self, owner_id, device_id, *, retire, start=None):
        query = _query(owner_id, device_id)
        with self._transaction(owner_id, write=True, read_committed=True) as session:
            latest = session.scalar(query.order_by(DeviceRevision.version.desc()).limit(1))
            if latest is None and retire:
                raise _not_found()
            origin = 0
            if start is not None:
                origin = cursor_value(start.cursor)
                self._require_anchor(session, owner_id, origin, start.witness)
                if latest is not None and latest.start_seq != origin:
                    raise LedgerError(
                        "sync_checkpoint_conflict", 409, "The registered start differs."
                    )
            if latest is not None:
                origin = latest.start_seq
                if not retire or latest.state == "retired":
                    return self._view(session, latest)
            row = DeviceRevision(
                owner_id=owner_id,
                device_id=device_id,
                version=2 if retire else 1,
                state="retired" if retire else "registered",
                previous_version=1 if retire else None,
                start_seq=origin,
            )
            session.add(row)
            session.flush()
            return self._view(session, row)
