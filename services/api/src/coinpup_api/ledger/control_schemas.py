"""Separate signed original-asset balances; never net unrelated dimensions."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel

ControlClass = Literal["receivable", "payable", "intercompany", "advance"]
ControlKey = Literal[
    "receivable.customer",
    "payable.supplier",
    "intercompany.receivable",
    "intercompany.payable",
    "advance.received",
    "advance.paid",
]
CONTROL_CLASSES = {
    "receivable.customer": "receivable",
    "payable.supplier": "payable",
    "intercompany.receivable": "intercompany",
    "intercompany.payable": "intercompany",
    "advance.received": "advance",
    "advance.paid": "advance",
}


class ControlBalance(BaseModel):
    account_id: UUID
    account_class: ControlClass
    system_key: ControlKey
    asset_id: str
    amount: str
    account_archived: bool
    asset_enabled: bool
    link_enabled: bool
    party_id: UUID | None
    counterparty_entity_id: UUID | None
    document_id: UUID | None
    document_line_id: UUID | None
