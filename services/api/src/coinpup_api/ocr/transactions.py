"""Private adapters for an already-open confirmation transaction; no alternate posting SQL."""

from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, SessionTransaction

from coinpup_api.files.service import DocumentService
from coinpup_api.ledger.models import AssetRecord, Entity, Ledger
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.service import LedgerError, _not_found
from coinpup_api.models import Administrator
from coinpup_api.sync.locking import acquire_write_lock


def _invalid():
    return LedgerError("ocr_transaction_invalid", 503, "The confirmation transaction is invalid.")


@dataclass(frozen=True)
class _Binding:
    session: Session
    transaction: SessionTransaction
    owner_id: UUID
    ledgers: frozenset[UUID]
    assets: frozenset[str]

    def check(self, owner_id):
        if (
            owner_id != self.owner_id
            or not self.session.is_active
            or self.session.get_transaction() is not self.transaction
            or self.session.in_nested_transaction()
        ):
            raise _invalid()

    def ledger(self, session, owner_id, ledger_id):
        self.check(owner_id)
        if session is not self.session or ledger_id not in self.ledgers:
            raise _invalid()


class _BoundTransaction:
    def __init__(self, binding):
        self._binding = binding
        super().__init__(binding.session.get_bind())

    @contextmanager
    def _transaction(self, owner_id, *, read_only=False, write=False):
        self._binding.check(owner_id)
        if read_only or not write:
            raise _invalid()
        yield self._binding.session

    def _locked_ledger(self, session, owner_id, ledger_id):
        self._binding.ledger(session, owner_id, ledger_id)
        return super()._locked_ledger(session, owner_id, ledger_id)

    def _locked_scope(self, session, owner_id, ledger_id):
        self._binding.ledger(session, owner_id, ledger_id)
        return super()._locked_scope(session, owner_id, ledger_id)

    def _assets(self, session, asset_ids):
        self._binding.check(self._binding.owner_id)
        if session is not self._binding.session or not set(asset_ids) <= self._binding.assets:
            raise _invalid()
        return super()._assets(session, asset_ids)


class _Posting(_BoundTransaction, PostingService):
    pass


class _Documents(_BoundTransaction, DocumentService):
    pass


class BoundCommands:
    """Expose only existing creation/link calls; revisions open their own service otherwise."""

    def __init__(self, binding):
        self._binding = binding
        self._posting = _Posting(binding)
        self._documents = _Documents(binding)

    def post(self, kind, ledger_id, payload, key, *, raw_body=None):
        if kind not in {"opening", "income", "expense", "transfer", "exchange"}:
            raise _invalid()
        self._binding.check(self._binding.owner_id)
        return getattr(self._posting, "post_" + kind)(
            self._binding.owner_id, ledger_id, payload, key, raw_body=raw_body
        )

    def link(self, ledger_id, operation_id, file_id):
        self._binding.check(self._binding.owner_id)
        return self._documents.link_file(self._binding.owner_id, ledger_id, operation_id, file_id)


def bind_commands(session, owner_id, ledger_ids, asset_ids=()):
    """Call after confirmation-receipt lookup, before acquiring any business row locks."""
    transaction = session.get_transaction()
    if transaction is None or not session.is_active or session.in_nested_transaction():
        raise _invalid()
    ledger_ids, asset_ids = frozenset(ledger_ids), frozenset(asset_ids)
    if not ledger_ids:
        raise _invalid()
    acquire_write_lock(session)
    if session.get(Administrator, owner_id) is None:
        raise _not_found()
    ledgers = session.scalars(
        select(Ledger)
        .where(Ledger.owner_id == owner_id, Ledger.id.in_(ledger_ids))
        .order_by(Ledger.id)
        .with_for_update()
    ).all()
    if len(ledgers) != len(ledger_ids):
        raise _not_found()
    entity_ids = {ledger.entity_id for ledger in ledgers}
    entities = session.scalars(
        select(Entity)
        .where(Entity.owner_id == owner_id, Entity.id.in_(entity_ids))
        .order_by(Entity.id)
        .with_for_update()
    ).all()
    if len(entities) != len(entity_ids):
        raise _not_found()
    if asset_ids:
        assets = session.scalars(
            select(AssetRecord)
            .where(AssetRecord.asset_id.in_(asset_ids))
            .order_by(AssetRecord.asset_id)
            .with_for_update(read=True)
        ).all()
        if len(assets) != len(asset_ids):
            raise _not_found()
    # Archive/enablement checks stay in the original commands, after their receipt replay.
    return BoundCommands(_Binding(session, transaction, owner_id, ledger_ids, asset_ids))
