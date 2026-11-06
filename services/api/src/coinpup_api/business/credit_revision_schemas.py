"""Unrouted credit revisions retain full prior facts and independent group/member versions."""

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

from coinpup_api.business.credit_schemas import CreditLineCreate, _receipt_quantity
from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting_schemas import CalendarDateFilter, QuantityText, RevisionCommand
from coinpup_api.ledger.schemas import AssetId, Command, Version

StoredVersion = Annotated[Version, Field(le=2147483647)]
Position = Annotated[StrictInt, Field(ge=1, le=200)]
FactReason = Annotated[StrictStr, Field(min_length=1, max_length=2000)]


class CreditReplacement(Command):
    credit_date: CalendarDateFilter
    reason: FactReason
    lines: list[CreditLineCreate] = Field(min_length=1, max_length=200)

    @field_validator("reason")
    @classmethod
    def explicit_reason(cls, value):
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("A non-empty credit reason without null characters is required")
        return value

    @model_validator(mode="after")
    def bounded_sources(self):
        if len({line.source_line_id for line in self.lines}) != len(self.lines):
            raise ValueError("Each original document line is named only once")
        for line in self.lines:
            _receipt_quantity(line.net_amount)
            _receipt_quantity(line.tax_amount)
            if not any(c in "123456789" for c in line.net_amount + line.tax_amount):
                raise ValueError("A replacement line must have a positive gross amount")
        return self


class CreditCorrectionCreate(RevisionCommand):
    expected_version: Annotated[Version, Field(le=2147483646)]
    replacement: CreditReplacement


class CreditCancellationCreate(RevisionCommand):
    expected_version: Annotated[Version, Field(le=2147483646)]


class CreditRevisionMember(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["create", "correct", "cancel"]
    source_line_id: UUID
    source_operation_id: UUID
    operation_id: UUID
    operation_version: StoredVersion
    previous_line_no: Position | None
    previous_operation_version: StoredVersion | None
    previous_journal_id: UUID | None
    reversal_journal_id: UUID | None
    previous_net_amount: QuantityText | None
    previous_tax_amount: QuantityText | None
    previous_total_amount: QuantityText | None
    previous_recognition_date: CalendarDateFilter | None
    line_no: Position | None
    journal_id: UUID | None
    net_amount: QuantityText | None
    tax_amount: QuantityText | None
    total_amount: QuantityText | None
    recognition_date: CalendarDateFilter | None

    @field_validator(
        "previous_net_amount",
        "previous_tax_amount",
        "previous_total_amount",
        "net_amount",
        "tax_amount",
        "total_amount",
    )
    @classmethod
    def bounded_quantities(cls, value):
        return None if value is None else _receipt_quantity(value)

    @model_validator(mode="after")
    def complete_transition(self):
        previous = (
            self.previous_line_no,
            self.previous_operation_version,
            self.previous_journal_id,
            self.reversal_journal_id,
            self.previous_net_amount,
            self.previous_tax_amount,
            self.previous_total_amount,
            self.previous_recognition_date,
        )
        current = (
            self.line_no,
            self.journal_id,
            self.net_amount,
            self.tax_amount,
            self.total_amount,
            self.recognition_date,
        )
        if any((v is not None) != (self.action != "create") for v in previous):
            raise ValueError(
                "Only existing members carry their complete previous fact and reversal"
            )
        if any((v is not None) != (self.action != "cancel") for v in current):
            raise ValueError("Only active replacement members carry complete new posting facts")
        if self.operation_version != (
            1 if self.action == "create" else self.previous_operation_version + 1
        ):
            raise ValueError("Member operation versions advance independently by exactly one")
        for total in (self.previous_total_amount, self.total_amount):
            if total is not None and not any(c in "123456789" for c in total):
                raise ValueError("Old and replacement posting totals must be positive")
        return self


class CreditRevisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["business_credit_revision"]
    credit_id: UUID
    revision_id: UUID
    ledger_id: UUID
    document_id: UUID
    asset_id: AssetId
    previous_version: Annotated[Version, Field(le=2147483646)]
    version: Annotated[StoredVersion, Field(ge=2)]
    action: Literal["correct", "cancel"]
    reason: Annotated[StrictStr, Field(min_length=1, max_length=1000)]
    previous_credit_date: CalendarDateFilter
    previous_credit_reason: FactReason
    credit_date: CalendarDateFilter | None
    replacement_reason: FactReason | None
    members: tuple[CreditRevisionMember, ...] = Field(min_length=1, max_length=400)

    @field_validator("reason", "previous_credit_reason", "replacement_reason")
    @classmethod
    def saved_reason(cls, value):
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("Saved reasons must contain text without null characters")
        return value

    @model_validator(mode="after")
    def complete_group(self):
        if self.version != self.previous_version + 1:
            raise ValueError("A group revision advances exactly one group version")
        if any(
            m.operation_version > self.version
            or (
                m.previous_operation_version is not None
                and m.previous_operation_version > self.previous_version
            )
            for m in self.members
        ):
            raise ValueError("A member cannot advance more times than its group")
        if (self.credit_date is not None) != (self.action == "correct") or (
            self.replacement_reason is not None
        ) != (self.action == "correct"):
            raise ValueError("Only correction carries new credit date and fact reason")
        old = [m for m in self.members if m.action != "create"]
        current = [m for m in self.members if m.action != "cancel"]
        removed = [m for m in self.members if m.action == "cancel"]
        if not 1 <= len(old) <= 200 or not (
            1 <= len(current) <= 200 if self.action == "correct" else not current
        ):
            raise ValueError("Previous and replacement group memberships must be complete")
        if sorted(m.previous_line_no for m in old) != list(range(1, len(old) + 1)):
            raise ValueError("Previous member positions must cover the complete previous group")
        if [m.line_no for m in current] != list(range(1, len(current) + 1)) or (
            list(self.members) != current + sorted(removed, key=lambda m: m.previous_line_no)
        ):
            raise ValueError("Transitions follow replacement order then removed previous order")
        for field in ("source_line_id", "source_operation_id", "operation_id"):
            if len({getattr(m, field) for m in self.members}) != len(self.members):
                raise ValueError("Transition source and member operation identities are distinct")
        if {m.source_operation_id for m in self.members} & {m.operation_id for m in self.members}:
            raise ValueError("Credit members cannot reuse source accrual operations")
        journals = [
            j
            for m in self.members
            for j in (m.previous_journal_id, m.reversal_journal_id, m.journal_id)
            if j is not None
        ]
        if len(set(journals)) != len(journals):
            raise ValueError("All old, reversed and new posting identities are distinct")
        return self


def validate_credit_revision_amounts(response: CreditRevisionResponse, asset: AssetDefinition):
    """Validate every frozen side with its actual full asset, preserving storage padding."""
    if not isinstance(asset, AssetDefinition) or asset.asset_id != response.asset_id:
        raise MoneyError("credit_revision_integrity", "The revision asset is inconsistent.")
    for member in response.members:
        for prefix in ("previous_", ""):
            values = [
                getattr(member, prefix + field)
                for field in ("net_amount", "tax_amount", "total_amount")
            ]
            if values[0] is None:
                continue
            net, tax, total = (Amount.from_decimal(Decimal(value), asset) for value in values)
            if (
                net.minor_units < 0
                or tax.minor_units < 0
                or total.minor_units <= 0
                or net + tax != total
            ):
                raise MoneyError(
                    "credit_revision_integrity", "The revision amounts are inconsistent."
                )
    return response
