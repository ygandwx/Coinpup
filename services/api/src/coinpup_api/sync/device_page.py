"""Validate an active device, its preceding evidence and one received page in one snapshot."""

from coinpup_api.ledger.service import LedgerError, _not_found
from coinpup_api.sync.device_models import DeviceRevision
from coinpup_api.sync.devices import _query
from coinpup_api.sync.schemas import cursor_value
from coinpup_api.sync.service import ChangeService


class DevicePageReads(ChangeService):
    def check(self, owner_id, device_id, *, prior, after, through, limit, received):
        with self._transaction(owner_id, read_only=True) as session:
            return self.check_in_snapshot(
                session,
                owner_id,
                device_id,
                prior=prior,
                after=after,
                through=through,
                limit=limit,
                received=received,
            )

    def check_in_snapshot(
        self, session, owner_id, device_id, *, prior, after, through, limit, received
    ):
        latest = session.scalar(
            _query(owner_id, device_id).order_by(DeviceRevision.version.desc()).limit(1)
        )
        if latest is None:
            raise _not_found()
        if latest.state != "registered":
            raise LedgerError("sync_device_retired", 409, "The device has been retired.")
        try:
            start = cursor_value(after)
        except ValueError:
            raise LedgerError("invalid_cursor", 422, "Invalid starting cursor.") from None
        self._require_anchor(session, owner_id, start, prior)
        return self._verify_in_snapshot(
            session, owner_id, after=after, through=through, limit=limit, received=received
        )
