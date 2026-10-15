"""Owned, snapshot-consistent settlement summaries in their original assets."""

from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import load_only

from coinpup_api.business.drafts import _fields
from coinpup_api.business.models import BusinessDocument, BusinessDocumentLine
from coinpup_api.business.settlement_models import (
    BusinessAllocation,
    BusinessAllocationLine,
    BusinessPayment,
)
from coinpup_api.business.settlement_read_schemas import (
    BusinessAllocationSummary,
    BusinessPaymentSummary,
    DocumentSettlementDetail,
    DocumentSettlementLine,
)
from coinpup_api.business.settlement_totals import applied_totals
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import AssetRecord
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.service import LedgerError, LedgerService, _not_found, _page


def integrity():
    return LedgerError("settlement_integrity", 503, "The stored settlement is inconsistent.")


def quantities(total, applied, asset):
    principal = Amount.from_decimal(total, asset)
    allocated = Amount.from_decimal(applied, asset)
    if not 0 <= allocated.minor_units <= principal.minor_units:
        raise integrity()
    return principal.to_string(), allocated.to_string(), (principal - allocated).to_string()


class BusinessSettlementBalances(LedgerService):
    def list_payments(self, owner_id, ledger_id, *, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            payment = BusinessPayment
            fields = set(BusinessPaymentSummary.model_fields) - {
                "applied_amount",
                "unapplied_amount",
                "fee_count",
            }
            rows = session.execute(
                select(payment, AssetRecord, func.jsonb_array_length(payment.fees))
                .options(load_only(*(getattr(payment, field) for field in fields)))
                .join(AssetRecord, AssetRecord.asset_id == payment.asset_id)
                .where(payment.ledger_id == ledger_id)
                .order_by(payment.created_at, payment.id)
                .limit(limit)
                .offset(offset)
            ).all()
            if not rows:
                return []
            applied = applied_totals(session, ledger_id, [row.id for row, _, _ in rows])
            result = []
            try:
                for row, asset, count in rows:
                    total, used, remaining = quantities(
                        row.amount, applied.get(row.id, Decimal(0)), asset_definition(asset)
                    )
                    result.append(
                        BusinessPaymentSummary(
                            **_fields(
                                row,
                                BusinessPaymentSummary,
                                amount=total,
                                applied_amount=used,
                                unapplied_amount=remaining,
                                fee_count=count,
                            )
                        )
                    )
            except (MoneyError, ValidationError):
                raise integrity() from None
            return result

    def list_allocations(self, owner_id, ledger_id, *, payment_id=None, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            group, payment, line = BusinessAllocation, BusinessPayment, BusinessAllocationLine
            statement = (
                select(group, payment.initial_allocation_id, AssetRecord)
                .join(payment, payment.id == group.payment_id)
                .join(AssetRecord, AssetRecord.asset_id == payment.asset_id)
                .where(group.ledger_id == ledger_id, payment.ledger_id == ledger_id)
                .order_by(group.created_at, group.id)
                .limit(limit)
                .offset(offset)
            )
            if payment_id is not None:
                statement = statement.where(group.payment_id == payment_id)
            rows = session.execute(statement).all()
            if not rows:
                return []
            totals = {
                r[0]: r[1:]
                for r in session.execute(
                    select(line.allocation_id, func.count(line.id), func.sum(line.payment_amount))
                    .where(
                        line.ledger_id == ledger_id,
                        line.allocation_id.in_([row.id for row, _, _ in rows]),
                    )
                    .group_by(line.allocation_id)
                )
            }
            result = []
            try:
                for row, initial_id, asset in rows:
                    if row.id not in totals:
                        raise integrity()
                    count, total = totals[row.id]
                    quantity = Amount.from_decimal(total, asset_definition(asset))
                    if quantity.minor_units <= 0:
                        raise integrity()
                    result.append(
                        BusinessAllocationSummary(
                            **_fields(
                                row,
                                BusinessAllocationSummary,
                                asset_id=asset.asset_id,
                                payment_amount=quantity.to_string(),
                                line_count=count,
                                is_initial=row.id == initial_id,
                            )
                        )
                    )
            except (MoneyError, ValidationError):
                raise integrity() from None
            return result

    def document_capacity(self, owner_id, ledger_id, document_id):
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
            document, asset_row = saved
            asset = asset_definition(asset_row)
            source = BusinessDocumentLine
            rows = session.scalars(
                select(source)
                .where(
                    source.document_id == document_id,
                    source.ledger_id == ledger_id,
                    source.archived.is_(False),
                )
                .order_by(source.line_no)
            ).all()
            applied = applied_totals(
                session, ledger_id, [row.id for row in rows], by_document_line=True
            )
            if not rows or not applied.keys() <= {row.id for row in rows}:
                raise integrity()
            try:
                lines = []
                total_units = applied_units = 0
                for row in rows:
                    used = applied.get(row.id, Decimal(0))
                    total, allocated, outstanding = quantities(row.total_amount, used, asset)
                    total_units += Amount.from_decimal(row.total_amount, asset).minor_units
                    applied_units += Amount.from_decimal(used, asset).minor_units
                    lines.append(
                        DocumentSettlementLine(
                            id=row.id,
                            line_no=row.line_no,
                            project_id=row.project_id,
                            total_amount=total,
                            applied_amount=allocated,
                            outstanding_amount=outstanding,
                        )
                    )
                return DocumentSettlementDetail(
                    id=document.id,
                    ledger_id=ledger_id,
                    party_id=document.party_id,
                    asset_id=document.asset_id,
                    total_amount=Amount(asset, total_units).to_string(),
                    applied_amount=Amount(asset, applied_units).to_string(),
                    outstanding_amount=Amount(asset, total_units - applied_units).to_string(),
                    lines=tuple(lines),
                )
            except (MoneyError, ValidationError):
                raise integrity() from None
