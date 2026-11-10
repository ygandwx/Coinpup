"""Owned snapshot reads of bounded history and one complete permanent 200 receipt."""

from pydantic import ValidationError
from sqlalchemy import Uuid, func, literal, select, union_all
from sqlalchemy.exc import IntegrityError

from coinpup_api.business.credit_models import BusinessCredit
from coinpup_api.business.credit_revision_history_schemas import CreditHistoryEntry
from coinpup_api.business.credit_revision_models import BusinessCreditRevision as Revision
from coinpup_api.business.credit_revision_receipts import integrity
from coinpup_api.business.credit_revision_schemas import (
    CreditRevisionResponse,
    validate_credit_revision_amounts,
)
from coinpup_api.business.models import BusinessDocument
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import AssetRecord, CommandReceipt
from coinpup_api.ledger.service import LedgerService, _not_found, _page


def _receipt(receipt, canonical, saved, asset):
    try:
        expected = CreditRevisionResponse.model_validate(canonical)
        if (
            receipt is None
            or receipt.hash_version != 2
            or receipt.response_status != 200
            or receipt.operation_id != expected.members[0].operation_id
            or canonical != expected.model_dump(mode="json")
            or receipt.response != canonical
            or expected.revision_id != saved.id
            or expected.credit_id != saved.credit_id
            or expected.ledger_id != saved.ledger_id
            or expected.document_id != saved.document_id
        ):
            raise integrity()
        original = CreditRevisionResponse.model_validate(receipt.response)
        return validate_credit_revision_amounts(original, asset)
    except (ValidationError, MoneyError):
        raise integrity() from None


def _revision(session, ledger_id, credit_id, revision_id):
    """Caller owns the authorized read snapshot; scope must precede the SQL response function."""
    found = session.execute(
        select(Revision, AssetRecord)
        .join(
            BusinessCredit,
            (BusinessCredit.id == Revision.credit_id)
            & (BusinessCredit.document_id == Revision.document_id)
            & (BusinessCredit.ledger_id == Revision.ledger_id),
        )
        .join(
            BusinessDocument,
            (BusinessDocument.id == Revision.document_id)
            & (BusinessDocument.ledger_id == Revision.ledger_id),
        )
        .join(AssetRecord, AssetRecord.asset_id == BusinessDocument.asset_id)
        .where(
            Revision.id == revision_id,
            Revision.credit_id == credit_id,
            Revision.ledger_id == ledger_id,
        )
    ).one_or_none()
    if found is None:
        raise _not_found()
    saved, stored_asset = found
    try:
        receipt, canonical = session.execute(
            select(CommandReceipt, func.coinpup_business_credit_revision_response(saved.id))
            .select_from(Revision)
            .outerjoin(
                CommandReceipt,
                (CommandReceipt.ledger_id == Revision.ledger_id)
                & (CommandReceipt.key == Revision.idempotency_key),
            )
            .where(Revision.id == saved.id, Revision.ledger_id == ledger_id)
        ).one()
        return _receipt(receipt, canonical, saved, asset_definition(stored_asset))
    except IntegrityError as error:
        if (
            getattr(error.orig, "sqlstate", None) == "23514"
            and getattr(getattr(error.orig, "diag", None), "constraint_name", None)
            == "ck_credit_revision_shape"
        ):
            raise integrity() from None
        raise


class BusinessCreditHistory(LedgerService):
    def list_history(self, owner_id, ledger_id, credit_id, *, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            if (
                session.scalar(
                    select(BusinessCredit.id).where(
                        BusinessCredit.id == credit_id, BusinessCredit.ledger_id == ledger_id
                    )
                )
                is None
            ):
                raise _not_found()
            directory = union_all(
                select(
                    BusinessCredit.version.label("version"),
                    literal(None, type_=Uuid).label("revision_id"),
                    literal("create").label("action"),
                    BusinessCredit.reason,
                    BusinessCredit.created_at,
                ).where(BusinessCredit.id == credit_id, BusinessCredit.ledger_id == ledger_id),
                select(
                    Revision.group_version,
                    Revision.id,
                    Revision.action,
                    Revision.revision_reason,
                    Revision.created_at,
                ).where(Revision.credit_id == credit_id, Revision.ledger_id == ledger_id),
            ).subquery()
            rows = session.execute(
                select(directory).order_by(directory.c.version).limit(limit).offset(offset)
            ).mappings()
            try:
                return [CreditHistoryEntry.model_validate(row) for row in rows]
            except ValidationError:
                raise integrity() from None

    def get_revision(self, owner_id, ledger_id, credit_id, revision_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            return _revision(session, ledger_id, credit_id, revision_id)
