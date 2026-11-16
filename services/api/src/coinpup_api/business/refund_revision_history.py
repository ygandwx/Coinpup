"""Scoped, repeatable reads of one refund version directory and its fixed receipts."""

from coinpup_api.business.refund_models import BusinessRefund
from coinpup_api.business.refund_revision_history_schemas import RefundHistoryEntry
from coinpup_api.business.refund_revision_models import BusinessRefundRevision as Revision
from coinpup_api.business.refund_revision_receipts import fixed_revision_receipt, integrity
from coinpup_api.business.settlement_revision_history import SettlementHistory
from coinpup_api.ledger.service import LedgerService, _page

_history = SettlementHistory(
    original=BusinessRefund,
    revision=Revision,
    parent_id=Revision.refund_id,
    version_column=Revision.operation_version,
    entry_type=RefundHistoryEntry,
    integrity=integrity,
    fixed_receipt=fixed_revision_receipt,
)


class BusinessRefundHistory(LedgerService):
    def list_history(self, owner_id, ledger_id, refund_id, *, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            return _history.page(session, ledger_id, refund_id, limit=limit, offset=offset)

    def get_revision(self, owner_id, ledger_id, refund_id, revision_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            return _history.receipt(session, ledger_id, refund_id, revision_id)
