"""Scoped, repeatable reads of one allocation return version directory and its fixed receipts."""

from coinpup_api.business.allocation_return_models import BusinessAllocationReturn
from coinpup_api.business.allocation_return_receipts import integrity
from coinpup_api.business.allocation_return_revision_history_schemas import (
    AllocationReturnHistoryEntry,
)
from coinpup_api.business.allocation_return_revision_models import (
    BusinessAllocationReturnRevision as Revision,
)
from coinpup_api.business.allocation_return_revision_receipts import fixed_revision_receipt
from coinpup_api.business.settlement_revision_history import SettlementHistory
from coinpup_api.ledger.service import LedgerService, _page

_history = SettlementHistory(
    original=BusinessAllocationReturn,
    revision=Revision,
    parent_id=Revision.return_id,
    version_column=Revision.group_version,
    entry_type=AllocationReturnHistoryEntry,
    integrity=integrity,
    fixed_receipt=fixed_revision_receipt,
)


class BusinessAllocationReturnHistory(LedgerService):
    def list_history(self, owner_id, ledger_id, return_id, *, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            return _history.page(session, ledger_id, return_id, limit=limit, offset=offset)

    def get_revision(self, owner_id, ledger_id, return_id, revision_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            return _history.receipt(session, ledger_id, return_id, revision_id)
