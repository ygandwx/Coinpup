"""The two-ledger command and immutable receipt contracts."""

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, StrictBool, StrictStr, field_validator

from coinpup_api.ledger.posting_schemas import CalendarDateFilter, QuantityText
from coinpup_api.ledger.schemas import AssetId, Command


class InterledgerSettings(Command):
    enabled: StrictBool


class _InterledgerCommand(Command):
    amount: QuantityText
    transaction_date: CalendarDateFilter
    description: Annotated[StrictStr, Field(max_length=2000)] = ""

    @field_validator("description")
    @classmethod
    def no_null_character(cls, value):
        if "\x00" in value:
            raise ValueError("Description contains an invalid null character")
        return value


class InterledgerAdvanceCreate(_InterledgerCommand):
    payer_ledger_id: UUID
    beneficiary_ledger_id: UUID
    payer_account_id: UUID
    expense_category_id: UUID
    asset_id: AssetId


class InterledgerRepaymentCreate(_InterledgerCommand):
    beneficiary_payment_account_id: UUID
    payer_receipt_account_id: UUID


class _Receipt(BaseModel):
    id: UUID
    amount: QuantityText
    transaction_date: CalendarDateFilter
    description: str
    payer_operation_id: UUID
    beneficiary_operation_id: UUID


class InterledgerAdvance(_Receipt):
    payer_ledger_id: UUID
    beneficiary_ledger_id: UUID
    payer_account_id: UUID
    expense_category_id: UUID
    asset_id: AssetId
    outstanding_amount: QuantityText


class InterledgerRepayment(_Receipt):
    advance_id: UUID
    beneficiary_payment_account_id: UUID
    payer_receipt_account_id: UUID


class InterledgerDetail(InterledgerAdvance):
    repayments: list[InterledgerRepayment]
