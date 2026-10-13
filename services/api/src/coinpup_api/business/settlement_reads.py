"""Owned immutable settlement detail and its exact original complete command receipt."""

from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import func, select

from coinpup_api.business.drafts import _fields
from coinpup_api.business.models import BusinessDocument, BusinessDocumentLine
from coinpup_api.business.settlement_balances import (
    BusinessSettlementBalances,
    integrity,
    quantities,
)
from coinpup_api.business.settlement_models import (
    BusinessAllocation,
    BusinessAllocationLine,
    BusinessPayment,
)
from coinpup_api.business.settlement_read_schemas import (
    BusinessAllocationDetail,
    BusinessAllocationLineDetail,
    BusinessPaymentDetail,
)
from coinpup_api.business.settlement_schemas import (
    AllocationPosting,
    BusinessAllocationResponse,
    BusinessPaymentResponse,
)
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import AssetRecord, CommandReceipt
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting_schemas import FeeResponse
from coinpup_api.ledger.service import _not_found


def _group_response(session, ledger_id, group):
    line = BusinessAllocationLine
    return BusinessAllocationResponse(
        kind="business_allocation",
        id=group.id,
        ledger_id=ledger_id,
        payment_id=group.payment_id,
        postings=tuple(
            AllocationPosting(**row)
            for row in session.execute(
                select(*(getattr(line, field) for field in AllocationPosting.model_fields))
                .where(line.ledger_id == ledger_id, line.allocation_id == group.id)
                .order_by(line.line_no)
            ).mappings()
        ),
    )


def _stored_receipt(session, ledger_id, key, expected, anchor):
    receipt = session.get(CommandReceipt, (ledger_id, key))
    if (
        receipt is None
        or receipt.hash_version != 2
        or receipt.response_status != 201
        or receipt.operation_id != anchor
    ):
        raise integrity()
    original = type(expected).model_validate(receipt.response)
    if original != expected:
        raise integrity()
    return original


def _payment_receipt(session, ledger_id, payment):
    initial = None
    if payment.initial_allocation_id is not None:
        group = session.scalar(
            select(BusinessAllocation).where(
                BusinessAllocation.id == payment.initial_allocation_id,
                BusinessAllocation.ledger_id == ledger_id,
                BusinessAllocation.payment_id == payment.id,
                BusinessAllocation.idempotency_key == payment.idempotency_key,
            )
        )
        if group is None:
            raise integrity()
        initial = _group_response(session, ledger_id, group)
    expected = BusinessPaymentResponse(
        kind="business_payment",
        id=payment.id,
        ledger_id=ledger_id,
        operation_id=payment.operation_id,
        journal_id=payment.journal_id,
        initial_allocation=initial,
    )
    return _stored_receipt(
        session, ledger_id, payment.idempotency_key, expected, payment.operation_id
    )


class BusinessSettlementReads(BusinessSettlementBalances):
    def get_payment(self, owner_id, ledger_id, payment_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            return self._payment_detail(session, ledger_id, payment_id)

    def _payment_detail(self, session, ledger_id, payment_id):
        saved = session.execute(
            select(BusinessPayment, AssetRecord)
            .join(AssetRecord, AssetRecord.asset_id == BusinessPayment.asset_id)
            .where(BusinessPayment.id == payment_id, BusinessPayment.ledger_id == ledger_id)
        ).one_or_none()
        if saved is None:
            raise _not_found()
        row, record = saved
        line = BusinessAllocationLine
        applied = session.scalar(
            select(func.sum(line.payment_amount)).where(
                line.ledger_id == ledger_id, line.payment_id == payment_id
            )
        )
        try:
            total, used, remaining = quantities(
                row.amount,
                applied if applied is not None else Decimal(0),
                asset_definition(record),
            )
            fees = tuple(FeeResponse.model_validate(fee) for fee in row.fees)
            if fees:
                assets = {
                    record.asset_id: asset_definition(record)
                    for record in session.scalars(
                        select(AssetRecord).where(
                            AssetRecord.asset_id.in_({fee.asset_id for fee in fees})
                        )
                    )
                }
                if assets.keys() != {fee.asset_id for fee in fees}:
                    raise integrity()
                fees = tuple(
                    fee.model_copy(
                        update={
                            "amount": Amount.from_decimal(
                                Decimal(fee.amount), assets[fee.asset_id]
                            ).to_string()
                        }
                    )
                    for fee in fees
                )
            return BusinessPaymentDetail(
                **_fields(
                    row,
                    BusinessPaymentDetail,
                    amount=total,
                    applied_amount=used,
                    unapplied_amount=remaining,
                    fee_count=len(fees),
                    fees=fees,
                    receipt=_payment_receipt(session, ledger_id, row),
                )
            )
        except (ValidationError, MoneyError):
            raise integrity() from None

    def get_allocation(self, owner_id, ledger_id, allocation_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            group, payment = BusinessAllocation, BusinessPayment
            saved = session.execute(
                select(group, payment, AssetRecord)
                .join(payment, payment.id == group.payment_id)
                .join(AssetRecord, AssetRecord.asset_id == payment.asset_id)
                .where(
                    group.ledger_id == ledger_id,
                    payment.ledger_id == ledger_id,
                    group.id == allocation_id,
                )
            ).one_or_none()
            if saved is None:
                raise _not_found()
            group, payment, record = saved
            asset = asset_definition(record)
            mapping, source, doc = BusinessAllocationLine, BusinessDocumentLine, BusinessDocument
            rows = session.execute(
                select(mapping, source, AssetRecord)
                .join(source, source.id == mapping.document_line_id)
                .join(doc, doc.id == mapping.document_id)
                .join(AssetRecord, AssetRecord.asset_id == doc.asset_id)
                .where(
                    mapping.ledger_id == ledger_id,
                    source.ledger_id == ledger_id,
                    doc.ledger_id == ledger_id,
                    mapping.allocation_id == allocation_id,
                )
                .order_by(mapping.line_no)
            ).all()
            try:
                lines, total = [], Amount(asset, 0)
                for row, source, source_asset in rows:
                    quantity = Amount.from_decimal(row.payment_amount, asset)
                    if quantity.minor_units <= 0 or row.document_amount <= 0:
                        raise integrity()
                    total += quantity
                    lines.append(
                        BusinessAllocationLineDetail(
                            **_fields(
                                row,
                                BusinessAllocationLineDetail,
                                payment_amount=quantity.to_string(),
                                document_asset_id=source_asset.asset_id,
                                document_amount=Amount.from_decimal(
                                    row.document_amount, asset_definition(source_asset)
                                ).to_string(),
                                project_id=source.project_id,
                                description=source.description,
                            )
                        )
                    )
                expected = BusinessAllocationResponse(
                    kind="business_allocation",
                    id=group.id,
                    ledger_id=ledger_id,
                    payment_id=payment.id,
                    postings=tuple(
                        AllocationPosting(
                            **{key: getattr(row, key) for key in AllocationPosting.model_fields}
                        )
                        for row in lines
                    ),
                )
                initial = payment.initial_allocation_id == group.id
                if initial:
                    receipt = _payment_receipt(session, ledger_id, payment)
                    if receipt.initial_allocation != expected:
                        raise integrity()
                else:
                    receipt = _stored_receipt(
                        session,
                        ledger_id,
                        group.idempotency_key,
                        expected,
                        expected.postings[0].operation_id,
                    )
                return BusinessAllocationDetail(
                    **_fields(
                        group,
                        BusinessAllocationDetail,
                        asset_id=asset.asset_id,
                        payment_amount=total.to_string(),
                        line_count=len(lines),
                        is_initial=initial,
                        lines=tuple(lines),
                        receipt=receipt,
                    )
                )
            except (ValidationError, MoneyError):
                raise integrity() from None
