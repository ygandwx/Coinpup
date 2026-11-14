"""Single-refund corrections, cancellations and immutable version-specific receipts."""

from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from coinpup_api.business.refund_schemas import RefundFeeReceipt, _receipt_quantity
from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting_schemas import (
    CalendarDateFilter,
    Fees,
    QuantityText,
    RevisionCommand,
)
from coinpup_api.ledger.schemas import AssetId, Command, Version

PriorVersion = Annotated[Version, Field(le=2147483646)]
FactReason = Annotated[StrictStr, Field(min_length=1, max_length=2000)]


def _distinct_returns(value):
    if len(set(value)) != len(value):
        raise ValueError("Each allocation return is referenced only once")
    return value


def _stored_reason(value):
    if not value.strip() or value != value.strip() or "\x00" in value:
        raise ValueError("Stored reasons must contain the original trimmed, non-empty text")
    return value


class RefundReplacement(Command):
    account_id: UUID
    asset_id: AssetId
    amount: QuantityText
    payment_amount: QuantityText
    transaction_date: CalendarDateFilter
    reason: FactReason
    fees: Fees = Field(default_factory=list)
    allocation_return_ids: list[UUID] = Field(default_factory=list, max_length=200)

    _positive = field_validator("amount", "payment_amount")(_receipt_quantity)
    _returns = field_validator("allocation_return_ids")(_distinct_returns)

    @field_validator("fees")
    @classmethod
    def positive_fees(cls, fees):
        for fee in fees:
            _receipt_quantity(fee.amount)
        return fees

    @field_validator("reason")
    @classmethod
    def explicit_fact_reason(cls, value):
        return _stored_reason(value.strip())


class RefundCorrectionCreate(RevisionCommand):
    expected_version: PriorVersion
    replacement: RefundReplacement


class RefundCancellationCreate(RevisionCommand):
    expected_version: PriorVersion


class RefundRevisionFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    account_id: UUID
    asset_id: AssetId
    amount: QuantityText
    payment_asset_id: AssetId
    payment_amount: QuantityText
    source_version: Annotated[Version, Field(le=1)]
    transaction_date: CalendarDateFilter
    recognition_date: CalendarDateFilter
    reason: FactReason
    fees: tuple[RefundFeeReceipt, ...] = Field(max_length=20)
    allocation_return_ids: tuple[UUID, ...] = Field(max_length=200)

    _positive = field_validator("amount", "payment_amount")(_receipt_quantity)
    _reason = field_validator("reason")(_stored_reason)
    _returns = field_validator("allocation_return_ids")(_distinct_returns)

    @model_validator(mode="after")
    def refund_date(self):
        if self.recognition_date != self.transaction_date:
            raise ValueError("A refund is recognized on its transaction date")
        return self


class RefundRevisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["business_refund_revision"]
    refund_id: UUID
    revision_id: UUID
    ledger_id: UUID
    payment_id: UUID
    operation_id: UUID
    previous_version: PriorVersion
    version: Annotated[Version, Field(le=2147483647)]
    action: Literal["correct", "cancel"]
    reason: Annotated[StrictStr, Field(min_length=1, max_length=1000)]
    previous_journal_id: UUID
    reversal_journal_id: UUID
    replacement_journal_id: UUID | None
    previous: RefundRevisionFact
    replacement: RefundRevisionFact | None

    _reason = field_validator("reason")(_stored_reason)

    @model_validator(mode="after")
    def consistent_revision(self):
        if self.version != self.previous_version + 1:
            raise ValueError("Refund operation versions must be consecutive")
        journals = [self.previous_journal_id, self.reversal_journal_id]
        if self.replacement_journal_id is not None:
            journals.append(self.replacement_journal_id)
        if len(set(journals)) != len(journals):
            raise ValueError("Previous, reversal and replacement journals must be distinct")
        correct = self.action == "correct"
        if (self.replacement is not None) != correct or (
            self.replacement_journal_id is not None
        ) != correct:
            raise ValueError("Only correction carries a replacement fact and posting")
        if self.replacement and self.previous.payment_asset_id != self.replacement.payment_asset_id:
            raise ValueError("A correction must retain the fixed payment's asset")
        return self


def refund_fact_amounts(fact: RefundRevisionFact, assets: Mapping[str, AssetDefinition]):
    """Decode the cash, fixed payment principal and ordered fees using their actual assets."""

    def decode(asset_id, quantity):
        asset = assets.get(asset_id)
        if not isinstance(asset, AssetDefinition) or asset.asset_id != asset_id:
            raise MoneyError("refund_revision_integrity", "The revision asset is inconsistent.")
        return Amount.from_decimal(Decimal(quantity), asset)

    cash = decode(fact.asset_id, fact.amount)
    principal = decode(fact.payment_asset_id, fact.payment_amount)
    if cash.asset == principal.asset and cash.minor_units != principal.minor_units:
        raise MoneyError("refund_revision_integrity", "Same-asset cash and principal differ.")
    return cash, principal, tuple(decode(fee.asset_id, fee.amount) for fee in fact.fees)


def validate_refund_revision_amounts(
    response: RefundRevisionResponse, assets: Mapping[str, AssetDefinition]
):
    for fact in (response.previous, response.replacement):
        if fact is not None:
            refund_fact_amounts(fact, assets)
    return response
