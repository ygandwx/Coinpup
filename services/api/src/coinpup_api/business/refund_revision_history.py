"""Scoped, repeatable reads of a refund version directory and one fixed revision receipt."""

from pydantic import ValidationError
from sqlalchemy import select

from coinpup_api.business.refund_models import BusinessRefund
from coinpup_api.business.refund_revision_history_schemas import RefundHistoryEntry
from coinpup_api.business.refund_revision_models import BusinessRefundRevision as Revision
from coinpup_api.business.refund_revision_receipts import fixed_revision_receipt, integrity
from coinpup_api.ledger.models import CommandReceipt
from coinpup_api.ledger.service import LedgerService, _not_found, _page


class BusinessRefundHistory(LedgerService):
    def list_history(self, owner_id, ledger_id, refund_id, *, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            original = (
                session.execute(
                    select(
                        BusinessRefund.version, BusinessRefund.reason, BusinessRefund.created_at
                    ).where(BusinessRefund.id == refund_id, BusinessRefund.ledger_id == ledger_id)
                )
                .mappings()
                .one_or_none()
            )
            if original is None:
                raise _not_found()
            entries = []
            try:
                if offset == 0:
                    entries.append(
                        RefundHistoryEntry(**original, revision_id=None, action="create")
                    )
                if len(entries) < limit:
                    # Contiguous versions allow an index seek by refund and operation version.
                    start = max(2, offset + 1)
                    rows = session.execute(
                        select(
                            Revision.operation_version.label("version"),
                            Revision.id.label("revision_id"),
                            Revision.action,
                            Revision.revision_reason.label("reason"),
                            Revision.created_at,
                        )
                        .where(
                            Revision.ledger_id == ledger_id,
                            Revision.refund_id == refund_id,
                            Revision.operation_version >= start,
                        )
                        .order_by(Revision.operation_version)
                        .limit(limit - len(entries))
                    ).mappings()
                    for version, row in enumerate(rows, start):
                        if row["version"] != version:
                            raise integrity()
                        entries.append(RefundHistoryEntry.model_validate(row))
                return entries
            except ValidationError:
                raise integrity() from None

    def get_revision(self, owner_id, ledger_id, refund_id, revision_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            row = session.execute(
                select(Revision, CommandReceipt)
                .outerjoin(
                    CommandReceipt,
                    (CommandReceipt.ledger_id == Revision.ledger_id)
                    & (CommandReceipt.key == Revision.idempotency_key),
                )
                .where(
                    Revision.id == revision_id,
                    Revision.refund_id == refund_id,
                    Revision.ledger_id == ledger_id,
                )
            ).one_or_none()
            if row is None:
                raise _not_found()
            return fixed_revision_receipt(session, *row)[0]
