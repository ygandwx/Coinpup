"""Contracts for one atomic deposit, withdrawal, or whole-group cancellation."""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from coinpup_api.ledger.posting_schemas import (
    FeeResponse,
    Fees,
    PostingMetadata,
    QuantityText,
    RevisionCommand,
)
from coinpup_api.ledger.schemas import AssetId

TransferKind = Literal["deposit", "withdrawal"]


class PlatformTransferCreate(PostingMetadata):
    kind: TransferKind
    source_account_id: UUID
    source_asset_id: AssetId
    destination_ledger_id: UUID
    destination_account_id: UUID
    destination_asset_id: AssetId
    amount: QuantityText
    fees: Fees = Field(default_factory=list)


class PlatformTransferCancel(RevisionCommand):
    pass


class PlatformPostingRef(BaseModel):
    ledger_id: UUID
    operation_id: UUID
    journal_id: UUID


class PlatformTransferCancellation(BaseModel):
    id: UUID
    source_ledger_id: UUID
    status: Literal["cancelled"]
    version: Literal[2]
    reason: str
    reversals: list[PlatformPostingRef]


class _PlatformTransferFacts(BaseModel):
    id: UUID
    kind: TransferKind
    source_ledger_id: UUID
    source_account_id: UUID
    source_asset_id: str
    source_amount: str
    destination_ledger_id: UUID
    destination_account_id: UUID
    destination_asset_id: str
    destination_amount: str
    transaction_date: date
    description: str
    fees: list[FeeResponse]
    operations: list[PlatformPostingRef]


class PlatformTransferReceipt(_PlatformTransferFacts):
    status: Literal["active"]
    version: Literal[1]


class PlatformTransferDetail(_PlatformTransferFacts):
    status: Literal["active", "cancelled"]
    version: Literal[1, 2]
    cancellation: PlatformTransferCancellation | None
