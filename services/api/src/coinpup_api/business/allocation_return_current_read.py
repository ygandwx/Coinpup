"""The effective return group and its ordered current postings in one authorized snapshot."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator
from sqlalchemy import select

from coinpup_api.business.allocation_return_receipts import integrity
from coinpup_api.business.allocation_return_revision_current import read_current_return
from coinpup_api.business.allocation_return_revision_schemas import ReturnGroupFact, StoredVersion
from coinpup_api.business.allocation_return_schemas import AllocationReturnPosting
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import AssetRecord
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.schemas import AssetId
from coinpup_api.ledger.service import LedgerService


class AllocationReturnCurrentMember(AllocationReturnPosting):
    operation_version: StoredVersion
    source_version: Annotated[StrictInt, Field(ge=1, le=1)]
    source_return_no: Annotated[StrictInt, Field(ge=1, le=9223372036854775807)]
    document_asset_id: AssetId
    project_id: UUID | None


class AllocationReturnCurrent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    return_id: UUID
    ledger_id: UUID
    payment_id: UUID
    party_id: UUID
    payment_direction: Literal["receipt", "payout"]
    payment_asset_id: AssetId
    version: StoredVersion
    status: Literal["active", "cancelled"]
    revision_id: UUID | None
    revision_reason: str | None = Field(min_length=1, max_length=1000)
    fact: ReturnGroupFact | None
    members: tuple[AllocationReturnCurrentMember, ...] = Field(max_length=200)

    @model_validator(mode="after")
    def validate_current_group(self):
        revised = self.version > 1
        active = self.status == "active"
        if (self.revision_id is not None) != revised or (
            self.revision_reason is not None
        ) != revised:
            raise ValueError("Only revised groups have a revision identifier and reason")
        if (
            bool(self.members) != active
            or (self.fact is not None) != active
            or (not active and not revised)
        ):
            raise ValueError("Only active groups have current members and business facts")
        if [m.line_no for m in self.members] != list(range(1, len(self.members) + 1)):
            raise ValueError("Current members must have complete consecutive positions")
        for key in (
            "source_allocation_line_id",
            "source_operation_id",
            "operation_id",
            "journal_id",
        ):
            if len({getattr(m, key) for m in self.members}) != len(self.members):
                raise ValueError("Each source, operation and journal must be unique")
        if any(m.operation_version > self.version for m in self.members):
            raise ValueError("The group version bounds all current member versions")
        return self


class BusinessAllocationReturnCurrentRead(LedgerService):
    def current(self, owner_id, ledger_id, return_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            state = read_current_return(session, ledger_id, return_id)
            payment = state.payment
            identifiers = {payment.asset_id, *(m.fact["document_asset_id"] for m in state.members)}
            catalog = {
                r.asset_id: asset_definition(r)
                for r in session.scalars(
                    select(AssetRecord).where(AssetRecord.asset_id.in_(identifiers))
                )
            }
            if catalog.keys() != identifiers:
                raise integrity()
            members = []
            try:
                for m in state.members:
                    fact = m.fact
                    principal = Amount.from_decimal(
                        fact["payment_amount"], catalog[payment.asset_id]
                    )
                    document = Amount.from_decimal(
                        fact["document_amount"], catalog[fact["document_asset_id"]]
                    )
                    if principal.asset == document.asset and principal != document:
                        raise integrity()
                    members.append(
                        AllocationReturnCurrentMember(
                            **{
                                k: fact[k]
                                for k in AllocationReturnCurrentMember.model_fields
                                if k not in {"payment_amount", "document_amount"}
                            },
                            payment_amount=principal.to_string(),
                            document_amount=document.to_string(),
                        )
                    )
                revision = state.revision
                current_fact = None
                if not state.cancelled:
                    current_fact = ReturnGroupFact(
                        return_date=revision.return_date if revision else state.group.return_date,
                        reason=revision.return_reason if revision else state.group.reason,
                    )
                return AllocationReturnCurrent(
                    return_id=state.group.id,
                    ledger_id=ledger_id,
                    payment_id=payment.id,
                    party_id=payment.party_id,
                    payment_direction=payment.direction,
                    payment_asset_id=payment.asset_id,
                    version=state.version,
                    status="cancelled" if state.cancelled else "active",
                    revision_id=revision.id if revision else None,
                    revision_reason=revision.revision_reason if revision else None,
                    fact=current_fact,
                    members=members,
                )
            except (MoneyError, ValidationError):
                raise integrity() from None
