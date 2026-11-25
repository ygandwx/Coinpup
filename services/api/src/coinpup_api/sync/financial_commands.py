"""Execute established financial commands inside the enclosing device write."""

from contextlib import contextmanager

from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.revisions import RevisionService
from coinpup_api.ledger.service import LedgerError
from coinpup_api.sync.financial_push import (
    FrozenFinancialPush,
    PushCancellation,
    PushCorrection,
)
from coinpup_api.sync.locking import acquire_write_lock


def _invalid_transaction():
    return LedgerError("invalid_sync_transaction", 503, "The sync write is unavailable.")


class _AttachedCommand:
    def __init__(self, session, owner_id, ledger_id, savepoint):
        super().__init__(session.get_bind())
        self._session = session
        self._owner_id = owner_id
        self._ledger_id = ledger_id
        self._transaction_root = session.get_transaction()
        self._savepoint = savepoint

    @contextmanager
    def _transaction(self, owner_id, *, write=False, read_only=False, read_committed=False):
        if (
            not write
            or read_only
            or owner_id != self._owner_id
            or not self._session.is_active
            or self._session.get_transaction() is not self._transaction_root
            or self._session.get_nested_transaction() is not self._savepoint
        ):
            raise _invalid_transaction()
        yield self._session

    def _locked_ledger(self, session, owner_id, ledger_id):
        if (session, owner_id, ledger_id) != (self._session, self._owner_id, self._ledger_id):
            raise _invalid_transaction()
        return super()._locked_ledger(session, owner_id, ledger_id)


class _AttachedPosting(_AttachedCommand, PostingService):
    pass


class _AttachedRevision(_AttachedCommand, RevisionService):
    pass


def apply_financial_push(session, owner_id, push: FrozenFinancialPush, key):
    """The caller commits the financial command, both receipts and any change notifications."""
    if not session.is_active or not session.in_transaction() or session.in_nested_transaction():
        raise _invalid_transaction()
    acquire_write_lock(session)

    command = push.command
    with session.begin_nested() as savepoint:
        # On rejection, roll back just the original command before recording conflict evidence.
        # The outer transaction and ordered change lock are still held.
        if isinstance(command, (PushCorrection, PushCancellation)):
            revisions = _AttachedRevision(session, owner_id, push.ledger_id, savepoint)
            run = revisions.correct if isinstance(command, PushCorrection) else revisions.cancel
            return run(
                owner_id,
                push.ledger_id,
                push.operation_id,
                command.payload,
                key,
                raw_body=push.original_payload,
            )
        posting = _AttachedPosting(session, owner_id, push.ledger_id, savepoint)
        run = getattr(posting, f"post_{command.kind}")
        return run(owner_id, push.ledger_id, command.payload, key, raw_body=push.original_payload)
