"""Snapshot-consistent return history and exact per-source remaining quantities."""

from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import func, select

from coinpup_api.business.allocation_document_facts import allocation_document_facts
from coinpup_api.business.allocation_return_models import (
    BusinessAllocationReturn as Return,
)
from coinpup_api.business.allocation_return_models import (
    BusinessAllocationReturnLine as Member,
)
from coinpup_api.business.allocation_return_projections import current_return_members
from coinpup_api.business.allocation_return_read_schemas import (
    AllocationReturnCapacity,
    AllocationReturnCapacityLine,
    AllocationReturnDetail,
    AllocationReturnLineDetail,
    AllocationReturnSummary,
)
from coinpup_api.business.allocation_return_receipts import integrity, response_from_rows
from coinpup_api.business.allocation_return_schemas import BusinessAllocationReturnResponse
from coinpup_api.business.drafts import _fields
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
                    asset,
                )
            )
    except (MoneyError, ValidationError):
        raise integrity() from None
    return result


def member_details(session, ledger_id, return_id, receipt, payment_asset):
    source = BusinessAllocationLine
    facts = allocation_document_facts(
        ledger_id, line_ids=[p.source_allocation_line_id for p in receipt.postings]
    )
    pinned = facts.c
    rows = session.execute(
        select(source, pinned.description, AssetRecord)
        .select_from(Member)
        .join(source, source.id == Member.source_allocation_line_id)
        .join(
            facts, (pinned.allocation_line_id == source.id) & (pinned.ledger_id == source.ledger_id)
        )
        .join(AssetRecord, AssetRecord.asset_id == pinned.asset_id)
        .where(
            Member.return_id == return_id,
            Member.ledger_id == ledger_id,
            source.ledger_id == ledger_id,
        )
        .order_by(Member.line_no)
    ).all()
    if len(rows) != len(receipt.postings):
        raise integrity()
    result = []
    for (source, description, asset), posting in zip(rows, receipt.postings, strict=True):
        if (
            source.id != posting.source_allocation_line_id
            or source.document_id != posting.document_id
            or source.document_line_id != posting.document_line_id
        ):
            raise integrity()
        result.append(
            AllocationReturnLineDetail(
                **(
                    posting.model_dump()
                    | dict(
                        source_allocation_id=source.allocation_id,
                        description=description,
                        document_asset_id=asset.asset_id,
                        payment_amount=Amount.from_decimal(
                            Decimal(posting.payment_amount), asset_definition(payment_asset)
                        ).to_string(),
                        document_amount=Amount.from_decimal(
                            Decimal(posting.document_amount), asset_definition(asset)
                        ).to_string(),
                    )
                )
            )
        )
    return tuple(result)


class BusinessAllocationReturnReads(LedgerService):
    def list_returns(self, owner_id, ledger_id, *, payment_id=None, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            return [
                item
                for _, item, _ in summaries(
                    session, ledger_id, payment_id=payment_id, limit=limit, offset=offset
                )
            ]

    def get_return(self, owner_id, ledger_id, return_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            rows = summaries(session, ledger_id, identifier=return_id)
            if not rows:
                raise _not_found()
            group, summary, payment_asset = rows[0]
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
                return AllocationReturnDetail(
                    **summary.model_dump(),
                    receipt=original,
                    lines=member_details(session, ledger_id, group.id, original, payment_asset),
                )
            except (ValidationError, MoneyError):
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
            mapping, operation = BusinessAllocationLine, FinancialOperation
            facts = allocation_document_facts(ledger_id, allocation_id=group.id)
            pinned = facts.c
            originals = session.execute(
                select(mapping, pinned.description, AssetRecord, operation)
                .select_from(mapping)
                .join(
                    facts,
                    (pinned.allocation_line_id == mapping.id)
                    & (pinned.ledger_id == mapping.ledger_id),
                )
                .join(AssetRecord, AssetRecord.asset_id == pinned.asset_id)
                .join(operation, operation.id == mapping.operation_id)
                .where(
                    mapping.allocation_id == group.id,
                    mapping.ledger_id == ledger_id,
                    operation.ledger_id == ledger_id,
                )
                .order_by(mapping.line_no)
            ).all()
            live = current_return_members(ledger_id, [row.id for row, *_ in originals]).c
            totals = {
                row[0]: row[1:]
                for row in session.execute(
                    select(
                        live.source_allocation_line_id,
                        func.sum(live.payment_amount),
                        func.sum(live.document_amount),
                    ).group_by(live.source_allocation_line_id)
                )
            }
            try:
                lines = []
                for row, description, asset, operation in originals:
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
                            description=description,
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
