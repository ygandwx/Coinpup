"""Owned reads of immutable confirmed documents and their complete original receipt."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import and_, func, select

from coinpup_api.business.confirmation_models import BusinessConfirmation, BusinessPosting
from coinpup_api.business.confirmation_schemas import (
    BusinessConfirmationResponse,
    ConfirmationPosting,
)
from coinpup_api.business.draft_schemas import BusinessDraftLineResponse, BusinessDraftSummary
from coinpup_api.business.drafts import AMOUNTS, DraftService, _fields, _response
from coinpup_api.business.models import BusinessDocument, BusinessDocumentLine
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import AssetRecord, CommandReceipt
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.service import LedgerError, LedgerService, _not_found, _page


class BusinessConfirmedSummary(BusinessDraftSummary):
    state: Literal["confirmed"]
    confirmation_id: UUID
    confirmed_at: datetime


class BusinessConfirmedDocument(BusinessConfirmedSummary):
    lines: list[BusinessDraftLineResponse]


class BusinessConfirmationDetail(BaseModel):
    document: BusinessConfirmedDocument
    confirmation: BusinessConfirmationResponse


def _integrity():
    return LedgerError("confirmation_integrity", 503, "The stored confirmation is inconsistent.")


def _documents(ledger_id):
    return (
        select(BusinessDocument)
        .join(
            BusinessConfirmation,
            and_(
                BusinessConfirmation.document_id == BusinessDocument.id,
                BusinessConfirmation.ledger_id == BusinessDocument.ledger_id,
            ),
        )
        .where(BusinessDocument.ledger_id == ledger_id, BusinessDocument.state == "confirmed")
    )


class BusinessConfirmationReads(LedgerService):
    def get_confirmation(self, owner_id, ledger_id, document_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            saved = session.execute(
                _documents(ledger_id)
                .add_columns(BusinessConfirmation)
                .where(BusinessDocument.id == document_id)
            ).one_or_none()
            if saved is None:
                raise _not_found()
            document, confirmation = saved
            receipt = session.get(CommandReceipt, (ledger_id, confirmation.idempotency_key))
            if receipt is None or receipt.hash_version != 2 or receipt.response_status != 201:
                raise _integrity()
            try:
                response = BusinessConfirmationResponse.model_validate(receipt.response)
                members = tuple(
                    ConfirmationPosting(**row)
                    for row in session.execute(
                        select(
                            BusinessPosting.line_id,
                            BusinessDocumentLine.line_no,
                            BusinessPosting.operation_id,
                            BusinessPosting.journal_id,
                        )
                        .join(
                            BusinessDocumentLine, BusinessDocumentLine.id == BusinessPosting.line_id
                        )
                        .where(
                            BusinessPosting.confirmation_id == confirmation.id,
                            BusinessPosting.ledger_id == ledger_id,
                        )
                        .order_by(BusinessDocumentLine.line_no)
                    ).mappings()
                )
                if (
                    response.id != confirmation.id
                    or response.ledger_id != ledger_id
                    or response.document_id != document_id
                    or response.source_version != confirmation.source_version
                    or response.document_version != document.version
                    or response.postings != members
                    or response.postings[0].operation_id != receipt.operation_id
                ):
                    raise _integrity()
                detail = _response(
                    document,
                    DraftService._lines(session, ledger_id, document_id),
                    asset_definition(session.get(AssetRecord, document.asset_id)),
                    response_type=BusinessConfirmedDocument,
                    confirmation_id=confirmation.id,
                    confirmed_at=confirmation.created_at,
                )
            except (ValidationError, MoneyError):
                raise _integrity() from None
            except LedgerError as error:
                if error.code == "draft_integrity":
                    raise _integrity() from None
                raise
            return BusinessConfirmationDetail(document=detail, confirmation=response)

    def list_confirmations(self, owner_id, ledger_id, *, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            documents = session.execute(
                _documents(ledger_id)
                .add_columns(BusinessConfirmation.id, BusinessConfirmation.created_at)
                .order_by(BusinessConfirmation.created_at, BusinessConfirmation.id)
                .limit(limit)
                .offset(offset)
            ).all()
            if not documents:
                return []
            line = BusinessDocumentLine
            totals = {
                row[0]: row[1:]
                for row in session.execute(
                    select(
                        line.document_id,
                        func.count(line.id),
                        *(func.sum(getattr(line, name + "_amount")) for name in AMOUNTS),
                    )
                    .where(
                        line.ledger_id == ledger_id,
                        line.document_id.in_([d.id for d, _, _ in documents]),
                        line.archived.is_(False),
                    )
                    .group_by(line.document_id)
                )
            }
            assets = {
                row.asset_id: asset_definition(row)
                for row in session.scalars(
                    select(AssetRecord).where(
                        AssetRecord.asset_id.in_({d.asset_id for d, _, _ in documents})
                    )
                )
            }
            result = []
            for document, confirmation_id, confirmed_at in documents:
                if document.id not in totals or document.asset_id not in assets:
                    raise _integrity()
                count, *values = totals[document.id]
                try:
                    amounts = {
                        name + "_amount": Amount.from_decimal(
                            value, assets[document.asset_id]
                        ).to_string()
                        for name, value in zip(AMOUNTS, values, strict=True)
                    }
                    result.append(
                        BusinessConfirmedSummary(
                            **_fields(
                                document,
                                BusinessConfirmedSummary,
                                line_count=count,
                                **amounts,
                                confirmation_id=confirmation_id,
                                confirmed_at=confirmed_at,
                            )
                        )
                    )
                except (ValidationError, MoneyError):
                    raise _integrity() from None
            return result
