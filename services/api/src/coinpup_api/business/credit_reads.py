"""Read immutable groups and bounded capacity in one ledger-owned snapshot."""

from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import select

from coinpup_api.business.credit_models import BusinessCredit
from coinpup_api.business.credit_read_schemas import (
    CreditCapacity,
    CreditDetail,
    CreditLineCapacity,
    CreditSummary,
)
from coinpup_api.business.credit_receipts import integrity, response_from_rows
from coinpup_api.business.credit_schemas import BusinessCreditResponse
from coinpup_api.business.drafts import _fields
from coinpup_api.business.models import BusinessDocument, BusinessDocumentLine
from coinpup_api.business.settlement_totals import document_totals
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import AssetRecord, CommandReceipt
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.service import LedgerService, _not_found, _page


def summaries(session, ledger_id, *, identifier=None, document_id=None, limit=100, offset=0):
    statement = (
        select(BusinessCredit, BusinessDocument)
        .join(
            BusinessDocument,
            (BusinessDocument.id == BusinessCredit.document_id)
            & (BusinessDocument.ledger_id == BusinessCredit.ledger_id),
        )
        .where(BusinessCredit.ledger_id == ledger_id)
        .order_by(BusinessCredit.created_at, BusinessCredit.id)
        .limit(limit)
        .offset(offset)
    )
    if identifier is not None:
        statement = statement.where(BusinessCredit.id == identifier)
    if document_id is not None:
        statement = statement.where(BusinessCredit.document_id == document_id)
    return [
        (
            row,
            CreditSummary(
                **_fields(
                    row,
                    CreditSummary,
                    document_kind=document.document_kind,
                    party_id=document.party_id,
                    asset_id=document.asset_id,
                )
            ),
        )
        for row, document in session.execute(statement)
    ]


class BusinessCreditReads(LedgerService):
    def list_credits(self, owner_id, ledger_id, *, document_id=None, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            return [
                summary
                for _, summary in summaries(
                    session, ledger_id, document_id=document_id, limit=limit, offset=offset
                )
            ]

    def get_credit(self, owner_id, ledger_id, credit_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            rows = summaries(session, ledger_id, identifier=credit_id)
            if not rows:
                raise _not_found()
            saved, summary = rows[0]
            expected = response_from_rows(session, saved.id)
            receipt = session.get(CommandReceipt, (ledger_id, saved.idempotency_key))
            if (
                receipt is None
                or receipt.hash_version != 2
                or receipt.response_status != 201
                or receipt.operation_id != expected.postings[0].operation_id
            ):
                raise integrity()
            try:
                original = BusinessCreditResponse.model_validate(receipt.response)
                if original != expected:
                    raise integrity()
                return CreditDetail(
                    **summary.model_dump(), source_version=saved.source_version, receipt=original
                )
            except ValidationError:
                raise integrity() from None

    def capacity(self, owner_id, ledger_id, document_id, *, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            saved = session.execute(
                select(BusinessDocument, AssetRecord)
                .join(AssetRecord, AssetRecord.asset_id == BusinessDocument.asset_id)
                .where(
                    BusinessDocument.id == document_id,
                    BusinessDocument.ledger_id == ledger_id,
                    BusinessDocument.state == "confirmed",
                )
            ).one_or_none()
            if saved is None:
                raise _not_found()
            document, stored_asset = saved
            asset = asset_definition(stored_asset)
            rows = session.scalars(
                select(BusinessDocumentLine)
                .where(
                    BusinessDocumentLine.document_id == document_id,
                    BusinessDocumentLine.ledger_id == ledger_id,
                    BusinessDocumentLine.archived.is_(False),
                    BusinessDocumentLine.total_amount > 0,
                )
                .order_by(BusinessDocumentLine.line_no, BusinessDocumentLine.id)
                .limit(limit)
                .offset(offset)
            ).all()
            totals = document_totals(session, ledger_id, {row.id for row in rows})
            result = []
            try:
                for row in rows:
                    net, tax, total, used, credited_net, credited_tax = (
                        Amount.from_decimal(value, asset)
                        for value in (
                            row.net_amount,
                            row.tax_amount,
                            row.total_amount,
                            *totals.get(row.id, (Decimal(0), Decimal(0), Decimal(0))),
                        )
                    )
                    if (
                        min(v.minor_units for v in (net, tax, used, credited_net, credited_tax)) < 0
                        or net + tax != total
                        or credited_net > net
                        or credited_tax > tax
                        or used + credited_net + credited_tax > total
                    ):
                        raise integrity()
                    result.append(
                        CreditLineCapacity(
                            source_line_id=row.id,
                            category_id=row.category_id,
                            project_id=row.project_id,
                            description=row.description,
                            recognition_date=row.recognition_date,
                            net_amount=net.to_string(),
                            tax_amount=tax.to_string(),
                            total_amount=total.to_string(),
                            credited_net_amount=credited_net.to_string(),
                            credited_tax_amount=credited_tax.to_string(),
                            applied_amount=used.to_string(),
                            remaining_net_amount=(net - credited_net).to_string(),
                            remaining_tax_amount=(tax - credited_tax).to_string(),
                            available_gross_amount=(
                                total - used - credited_net - credited_tax
                            ).to_string(),
                        )
                    )
                return CreditCapacity(
                    **_fields(
                        document, CreditCapacity, expected_version=document.version, lines=result
                    )
                )
            except (MoneyError, ValidationError):
                raise integrity() from None
