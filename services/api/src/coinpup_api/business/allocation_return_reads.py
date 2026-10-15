"""Snapshot-consistent return history and exact per-source remaining quantities."""

from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import func, select

from coinpup_api.business.allocation_return_models import (
    BusinessAllocationReturn as Return,
)
from coinpup_api.business.allocation_return_models import (
    BusinessAllocationReturnLine as Member,
)
from coinpup_api.business.allocation_return_read_schemas import (
    AllocationReturnCapacity,
    AllocationReturnCapacityLine,
    AllocationReturnDetail,
    AllocationReturnSummary,
)
from coinpup_api.business.allocation_return_receipts import integrity, response_from_rows
from coinpup_api.business.allocation_return_schemas import BusinessAllocationReturnResponse
from coinpup_api.business.drafts import _fields
from coinpup_api.business.models import BusinessDocument, BusinessDocumentLine
from coinpup_api.business.settlement_balances import quantities
from coinpup_api.business.settlement_models import (
    BusinessAllocation,
    BusinessAllocationLine,
    BusinessPayment,
)
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import AssetRecord, CommandReceipt, FinancialOperation
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.service import LedgerService, _not_found, _page


def summaries(session, ledger_id, *, identifier=None, payment_id=None, limit=100, offset=0):
    statement = (
        select(Return, AssetRecord)
        .select_from(Return)
        .join(BusinessPayment, BusinessPayment.id == Return.payment_id)
        .join(AssetRecord, AssetRecord.asset_id == BusinessPayment.asset_id)
        .where(Return.ledger_id == ledger_id, BusinessPayment.ledger_id == ledger_id)
        .order_by(Return.created_at, Return.id)
        .limit(limit)
        .offset(offset)
    )
    if identifier is not None:
        statement = statement.where(Return.id == identifier)
    if payment_id is not None:
        statement = statement.where(Return.payment_id == payment_id)
    rows = session.execute(statement).all()
    if not rows:
        return []
    totals = {
        row[0]: row[1:]
        for row in session.execute(
            select(Member.return_id, func.count(Member.id), func.sum(Member.payment_amount))
            .where(Member.ledger_id == ledger_id, Member.return_id.in_([row.id for row, _ in rows]))
            .group_by(Member.return_id)
        )
    }
    result = []
    try:
        for row, asset in rows:
            if row.id not in totals:
                raise integrity()
            count, total = totals[row.id]
            amount = Amount.from_decimal(total, asset_definition(asset))
            if amount.minor_units <= 0:
                raise integrity()
            result.append(
                (
                    row,
                    AllocationReturnSummary(
                        **_fields(
                            row,
                            AllocationReturnSummary,
                            asset_id=asset.asset_id,
                            payment_amount=amount.to_string(),
                            line_count=count,
                        )
                    ),
                )
            )
    except (MoneyError, ValidationError):
        raise integrity() from None
    return result


class BusinessAllocationReturnReads(LedgerService):
    def list_returns(self, owner_id, ledger_id, *, payment_id=None, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            return [
                item
                for _, item in summaries(
                    session, ledger_id, payment_id=payment_id, limit=limit, offset=offset
                )
            ]

    def get_return(self, owner_id, ledger_id, return_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            rows = summaries(session, ledger_id, identifier=return_id)
            if not rows:
                raise _not_found()
            group, summary = rows[0]
            receipt = session.get(CommandReceipt, (ledger_id, group.idempotency_key))
            expected = response_from_rows(session, group.id)
            if (
                receipt is None
                or receipt.hash_version != 2
                or receipt.response_status != 201
                or receipt.operation_id != expected.postings[0].operation_id
            ):
                raise integrity()
            try:
                original = BusinessAllocationReturnResponse.model_validate(receipt.response)
                if original != expected:
                    raise integrity()
                return AllocationReturnDetail(**summary.model_dump(), receipt=original)
            except ValidationError:
                raise integrity() from None

    def capacity(self, owner_id, ledger_id, allocation_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            saved = session.execute(
                select(BusinessAllocation, AssetRecord)
                .select_from(BusinessAllocation)
                .join(BusinessPayment, BusinessPayment.id == BusinessAllocation.payment_id)
                .join(AssetRecord, AssetRecord.asset_id == BusinessPayment.asset_id)
                .where(
                    BusinessAllocation.id == allocation_id,
                    BusinessAllocation.ledger_id == ledger_id,
                    BusinessPayment.ledger_id == ledger_id,
                )
            ).one_or_none()
            if saved is None:
                raise _not_found()
            group, payment_asset = saved
            mapping, source, doc, operation = (
                BusinessAllocationLine,
                BusinessDocumentLine,
                BusinessDocument,
                FinancialOperation,
            )
            originals = session.execute(
                select(mapping, source, AssetRecord, operation)
                .select_from(mapping)
                .join(source, source.id == mapping.document_line_id)
                .join(doc, doc.id == mapping.document_id)
                .join(AssetRecord, AssetRecord.asset_id == doc.asset_id)
                .join(operation, operation.id == mapping.operation_id)
                .where(
                    mapping.allocation_id == group.id,
                    mapping.ledger_id == ledger_id,
                    source.ledger_id == ledger_id,
                    doc.ledger_id == ledger_id,
                    operation.ledger_id == ledger_id,
                )
                .order_by(mapping.line_no)
            ).all()
            totals = {
                row[0]: row[1:]
                for row in session.execute(
                    select(
                        Member.source_allocation_line_id,
                        func.sum(Member.payment_amount),
                        func.sum(Member.document_amount),
                    )
                    .where(
                        Member.ledger_id == ledger_id,
                        Member.source_allocation_line_id.in_([row.id for row, *_ in originals]),
                    )
                    .group_by(Member.source_allocation_line_id)
                )
            }
            try:
                lines = []
                for row, source, asset, operation in originals:
                    paid, applied = totals.get(row.id, (Decimal(0), Decimal(0)))
                    payment, returned_payment, remaining_payment = quantities(
                        row.payment_amount, paid, asset_definition(payment_asset)
                    )
                    document, returned_document, remaining_document = quantities(
                        row.document_amount, applied, asset_definition(asset)
                    )
                    lines.append(
                        AllocationReturnCapacityLine(
                            source_allocation_line_id=row.id,
                            source_operation_id=row.operation_id,
                            expected_version=operation.version,
                            source_status=operation.status,
                            document_id=row.document_id,
                            document_line_id=row.document_line_id,
                            document_asset_id=asset.asset_id,
                            description=source.description,
                            payment_amount=payment,
                            document_amount=document,
                            returned_payment_amount=returned_payment,
                            returned_document_amount=returned_document,
                            remaining_payment_amount=remaining_payment,
                            remaining_document_amount=remaining_document,
                        )
                    )
                return AllocationReturnCapacity(
                    id=group.id,
                    ledger_id=ledger_id,
                    payment_id=group.payment_id,
                    asset_id=payment_asset.asset_id,
                    allocation_date=group.allocation_date,
                    lines=tuple(lines),
                )
            except (ValidationError, MoneyError):
                raise integrity() from None
