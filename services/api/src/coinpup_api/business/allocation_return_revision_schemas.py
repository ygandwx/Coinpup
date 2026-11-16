"""Immutable two-asset facts and whole-group transitions for allocation returns."""

from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from coinpup_api.business.allocation_return_schemas import AllocationReturnLineCreate
from coinpup_api.business.refund_schemas import _receipt_quantity
from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting_schemas import CalendarDateFilter, QuantityText, RevisionCommand
from coinpup_api.ledger.schemas import AssetId, Command, Version

PriorVersion = Annotated[Version, Field(le=2147483646)]
StoredVersion = Annotated[Version, Field(le=2147483647)]
Position = Annotated[StrictInt, Field(ge=1, le=200)]
FactReason = Annotated[StrictStr, Field(min_length=1, max_length=2000)]


def _stored_text(value):
    if not value or value != value.strip() or "\x00" in value:
        raise ValueError("Stored reasons must contain trimmed text without null characters")
    return value


class ReturnReplacementLine(AllocationReturnLineCreate):
    expected_version: Annotated[Version, Field(le=1)]
    _positive = field_validator("payment_amount", "document_amount")(_receipt_quantity)


class ReturnReplacement(Command):
    return_date: CalendarDateFilter
    reason: FactReason
    lines: list[ReturnReplacementLine] = Field(min_length=1, max_length=200)

    @field_validator("reason")
    @classmethod
    def explicit_reason(cls, value):
        return _stored_text(value.strip())

    @model_validator(mode="after")
    def distinct_sources(self):
        if len({line.source_allocation_line_id for line in self.lines}) != len(self.lines):
            raise ValueError("Each original allocation line can occur only once")
        return self


class AllocationReturnCorrectionCreate(RevisionCommand):
    expected_version: PriorVersion
    replacement: ReturnReplacement


class AllocationReturnCancellationCreate(RevisionCommand):
    expected_version: PriorVersion


class ReturnPostingFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    line_no: Position
    operation_version: StoredVersion
    journal_id: UUID
    payment_amount: QuantityText
    document_amount: QuantityText
    _positive = field_validator("payment_amount", "document_amount")(_receipt_quantity)


class ReturnMemberTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["create", "correct", "cancel"]
    source_allocation_line_id: UUID
    source_operation_id: UUID
    source_version: Annotated[Version, Field(le=1)]
    source_return_no: Annotated[StrictInt, Field(ge=1, le=9223372036854775807)]
    document_id: UUID
    document_line_id: UUID
    document_asset_id: AssetId
    operation_id: UUID
    operation_version: StoredVersion
    previous: ReturnPostingFact | None
    reversal_journal_id: UUID | None
    replacement: ReturnPostingFact | None

    @model_validator(mode="after")
    def complete_transition(self):
        if (self.previous is not None) != (self.action != "create") or (
            self.reversal_journal_id is not None
        ) != (self.previous is not None):
            raise ValueError("Existing members must carry their previous posting and reversal")
        if (self.replacement is not None) != (self.action != "cancel"):
            raise ValueError("Only active members have a replacement posting")
        if self.operation_version != (self.previous.operation_version + 1 if self.previous else 1):
            raise ValueError("Member operations start at one and advance by exactly one")
        if self.replacement and self.replacement.operation_version != self.operation_version:
            raise ValueError("Replacement posting must have the new member version")
        return self


class ReturnGroupFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    return_date: CalendarDateFilter
    reason: FactReason
    _reason = field_validator("reason")(_stored_text)


class AllocationReturnRevisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["business_allocation_return_revision"]
    return_id: UUID
    revision_id: UUID
    ledger_id: UUID
    payment_id: UUID
    payment_asset_id: AssetId
    previous_version: PriorVersion
    version: Annotated[StoredVersion, Field(ge=2)]
    action: Literal["correct", "cancel"]
    reason: Annotated[StrictStr, Field(min_length=1, max_length=1000)]
    previous: ReturnGroupFact
    replacement: ReturnGroupFact | None
    members: tuple[ReturnMemberTransition, ...] = Field(min_length=1, max_length=400)
    _reason = field_validator("reason")(_stored_text)

    @model_validator(mode="after")
    def complete_group(self):
        if self.version != self.previous_version + 1 or any(
            member.operation_version > self.version for member in self.members
        ):
            raise ValueError("Group versions must be consecutive and bound all member versions")
        if (self.replacement is not None) != (self.action == "correct"):
            raise ValueError("Only correction supplies a new complete group fact")
        old = [m.previous.line_no for m in self.members if m.previous]
        active = [m.replacement.line_no for m in self.members if m.replacement]
        removed = [m.previous.line_no for m in self.members if not m.replacement]
        if not old or sorted(old) != list(range(1, len(old) + 1)):
            raise ValueError("The previous group must cover all original positions")
        if bool(active) != bool(self.replacement) or active != list(range(1, len(active) + 1)):
            raise ValueError("Active members must cover the ordered replacement")
        if any(not m.replacement for m in self.members[: len(active)]) or removed != sorted(
            removed
        ):
            raise ValueError("Removed members follow active members in previous order")
        for key in ("source_allocation_line_id", "source_operation_id", "operation_id"):
            if len({getattr(m, key) for m in self.members}) != len(self.members):
                raise ValueError("Source and member mappings must be unique within a group")
        if {m.source_operation_id for m in self.members} & {m.operation_id for m in self.members}:
            raise ValueError("Return postings cannot reuse allocation operations")
        journals = []
        for member in self.members:
            if member.previous:
                journals.extend((member.previous.journal_id, member.reversal_journal_id))
            if member.replacement:
                journals.append(member.replacement.journal_id)
        if len(set(journals)) != len(journals):
            raise ValueError("Distinct journal identities are required for all posting facts")
        return self


def validate_return_revision_amounts(
    receipt: AllocationReturnRevisionResponse, catalog: Mapping[str, AssetDefinition]
):
    """Decode each side at its actual asset scale, including inactive historical assets."""
    payment = catalog.get(receipt.payment_asset_id)
    for member in receipt.members:
        document = catalog.get(member.document_asset_id)
        if (
            not isinstance(payment, AssetDefinition)
            or payment.asset_id != receipt.payment_asset_id
            or not isinstance(document, AssetDefinition)
            or document.asset_id != member.document_asset_id
        ):
            raise MoneyError(
                "allocation_return_integrity", "Missing original return asset identity."
            )
        for fact in (member.previous, member.replacement):
            if fact is None:
                continue
            p = Amount.from_decimal(Decimal(fact.payment_amount), payment)
            d = Amount.from_decimal(Decimal(fact.document_amount), document)
            if payment == document and p != d:
                raise MoneyError("allocation_return_integrity", "Same-asset sides must be equal.")
    return receipt
