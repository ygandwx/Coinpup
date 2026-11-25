"""Read the immutable sides of a stale device revision in one scoped snapshot."""

from pydantic import BaseModel
from sqlalchemy import select

from coinpup_api.ledger.models import FinancialOperation
from coinpup_api.ledger.posting_schemas import HistoryEntry
from coinpup_api.ledger.readers import LEGACY_OPERATION_KINDS
from coinpup_api.ledger.revisions import history_at_versions
from coinpup_api.ledger.service import LedgerError, _not_found
from coinpup_api.sync.financial_push import FrozenFinancialPush, PushCorrection


class FinancialConflictEvidence(BaseModel):
    basis: HistoryEntry
    competing: HistoryEntry


def _inconsistent():
    return LedgerError("ledger_integrity", 503, "Stored financial history is inconsistent.")


def _witness(entry):
    keyed = {journal.kind: journal for journal in entry.journals}
    posting, reversal = keyed.get("posting"), keyed.get("reversal")
    if len(keyed) != len(entry.journals) or (
        (entry.version == 1 and (posting is None or reversal is not None))
        or (entry.version > 1 and (reversal is None or reversal.reverses_journal_id is None))
        or (posting is not None and posting.reverses_journal_id is not None)
    ):
        raise _inconsistent()
    return posting, reversal


def competing_versions(session, push: FrozenFinancialPush) -> FinancialConflictEvidence | None:
    """Called only after authorized revision rejection, inside the same serialized write."""
    basis = push.expected_version
    if basis is None:
        return None
    operation = session.scalar(
        select(FinancialOperation)
        .where(
            FinancialOperation.ledger_id == push.ledger_id,
            FinancialOperation.id == push.operation_id,
            FinancialOperation.kind.in_(LEGACY_OPERATION_KINDS),
        )
        .with_for_update()
    )
    if operation is None:
        raise _not_found()
    if basis >= operation.version:
        # A future version or an already-known tombstone does not identify two versions.
        return None

    if isinstance(push.command, PushCorrection) and push.command.payload.replacement.kind != (
        operation.kind
    ):
        return None

    versions = history_at_versions(
        session, operation, (basis, operation.version), require_sealed=True
    )
    if [entry.version for entry in versions] != [basis, operation.version]:
        raise _inconsistent()
    first, last = versions
    first_posting, _ = _witness(first)
    last_posting, last_reversal = _witness(last)
    expected_journal = (
        last_posting.id
        if last_posting
        else (last_reversal.reverses_journal_id if last_reversal else None)
    )
    if (
        first_posting is None
        or (last.action == "cancel") != (operation.status == "cancelled")
        or operation.current_journal_id != expected_journal
        or (
            last.version == first.version + 1
            and (last_reversal is None or last_reversal.reverses_journal_id != first_posting.id)
        )
    ):
        raise _inconsistent()
    return FinancialConflictEvidence(basis=first, competing=last)
