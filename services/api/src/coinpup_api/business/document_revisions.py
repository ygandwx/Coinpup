"""Authorized whole-document commands preserve old intentions before validating a new change."""

from types import SimpleNamespace

from sqlalchemy import select

from coinpup_api.business.confirmation_hash import document_revision_hash_v2
from coinpup_api.business.document_fixed_receipts import replay_document_revision
from coinpup_api.business.document_revision_capacity import check_document_revision_capacity
from coinpup_api.business.document_revision_journals import prepare_document_revision_journals
from coinpup_api.business.document_revision_references import reviewed_document_references
from coinpup_api.business.document_revision_storage import persist_document_revision
from coinpup_api.business.document_revision_versions import read_confirmed_document_version
from coinpup_api.ledger.balances import BalanceQueries
from coinpup_api.ledger.common import PostingCore, _invalid_money, asset_definition
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.idempotency import _IDEMPOTENCY_KEY
from coinpup_api.ledger.models import AssetRecord, CommandReceipt
from coinpup_api.ledger.period_checks import require_open_period
from coinpup_api.ledger.service import LedgerError, _not_found, _version


class BusinessDocumentRevisionService(BalanceQueries, PostingCore):
    def correct(self, owner_id, ledger_id, document_id, payload, key, *, raw_body=None):
        return self._revise(owner_id, ledger_id, document_id, "correct", payload, key, raw_body)

    def cancel(self, owner_id, ledger_id, document_id, payload, key, *, raw_body=None):
        return self._revise(owner_id, ledger_id, document_id, "cancel", payload, key, raw_body)

    def _revise(self, owner_id, ledger_id, document_id, action, payload, key, raw_body):
        if not isinstance(key, str) or _IDEMPOTENCY_KEY.fullmatch(key) is None:
            raise LedgerError("invalid_idempotency_key", 422, "Use 1–128 visible ASCII characters.")
        with self._transaction(owner_id, write=True, read_committed=True) as session:
            _, entity = self._locked_ledger(session, owner_id, ledger_id)
            digest = document_revision_hash_v2(
                action, ledger_id, document_id, payload, raw_body=raw_body
            )
            receipt = session.get(CommandReceipt, (ledger_id, key))
            if receipt is not None:
                return replay_document_revision(
                    session, receipt, ledger_id, document_id, action, payload, digest
                )
            if entity.archived:
                raise LedgerError("entity_archived", 409, "Restore the entity before posting.")
            current = read_confirmed_document_version(session, ledger_id, document_id)
            _version(current, payload.expected_version)
            if current.document is None:
                raise LedgerError("document_cancelled", 409, "A cancelled document is terminal.")
            if current.version >= 2147483647:
                raise LedgerError(
                    "document_revision_version", 409, "The document version cannot advance."
                )
            document = current.document
            proposed = payload.replacement if action == "correct" else None
            assets = {document.asset_id}
            if proposed is not None:
                assets.add(proposed.asset_id)
            records = session.scalars(
                select(AssetRecord)
                .where(AssetRecord.asset_id.in_(assets))
                .order_by(AssetRecord.asset_id)
                .with_for_update(read=True)
            ).all()
            if len(records) != len(assets):
                raise _not_found()
            catalog = {record.asset_id: asset_definition(record) for record in records}
            old_asset = catalog[document.asset_id]
            new_asset = catalog[proposed.asset_id] if proposed is not None else None
            if new_asset is not None and not new_asset.enabled:
                raise LedgerError("asset_disabled", 409, "An asset is disabled for new use.")
            try:
                references = (
                    reviewed_document_references(session, ledger_id, document, proposed)
                    if proposed is not None
                    else None
                )
                check_document_revision_capacity(
                    session, ledger_id, document, old_asset, proposed, new_asset
                )
                if proposed is not None:
                    # Both identities are locked. Add the replacement control link after pricing.
                    control = (
                        "receivable.customer"
                        if document.document_kind == "invoice"
                        else "payable.supplier"
                    )
                    ControlAccounts(self.engine).ensure(
                        session, owner_id, ledger_id, {control: {proposed.asset_id}}
                    )
                journals = prepare_document_revision_journals(
                    session, document, payload, current.postings, old_asset, new_asset
                )
                # Include old/new issue days and every recognition day, including zero lines.
                require_open_period(
                    session,
                    ledger_id,
                    *(
                        SimpleNamespace(transaction_date=day, recognition_date=day)
                        for day in journals.affected_dates
                    ),
                )
                self._validate_balance_deltas(
                    session,
                    ledger_id,
                    [
                        line
                        for member in journals.members
                        for journal in (member.reversal, member.replacement)
                        if journal is not None
                        for line in journal.lines
                    ],
                    catalog,
                )
                return persist_document_revision(
                    session,
                    owner_id,
                    current,
                    payload,
                    key,
                    digest,
                    references,
                    journals,
                    new_asset,
                )
            except MoneyError as error:
                raise _invalid_money(error) from None
