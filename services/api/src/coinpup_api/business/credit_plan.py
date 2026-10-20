"""Pure manual net/tax credit plans; no persistence, cash or inferred tax rates."""

from dataclasses import dataclass
from uuid import UUID

from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount


@dataclass(frozen=True, slots=True)
class CreditSource:
    ledger_id: UUID
    document_id: UUID
    line_id: UUID
    document_kind: str
    category_id: UUID
    project_id: UUID | None
    net: Amount
    tax: Amount
    total: Amount
    credited_net: Amount
    credited_tax: Amount


@dataclass(frozen=True, slots=True)
class CreditLinePlan:
    document_id: UUID
    line_id: UUID
    category_id: UUID
    project_id: UUID | None
    net: Amount
    tax: Amount
    total: Amount
    remaining_net: Amount
    remaining_tax: Amount
    category_role: str
    category_amount: Amount
    control_key: str
    control_amount: Amount


def plan_credit_line(ledger_id, source: CreditSource, net: str, tax: str) -> CreditLinePlan:
    if (
        not isinstance(source, CreditSource)
        or not isinstance(ledger_id, UUID)
        or source.ledger_id != ledger_id
        or source.document_kind not in {"invoice", "bill"}
        or any(
            not isinstance(value, UUID)
            for value in (source.document_id, source.line_id, source.category_id)
        )
        or (source.project_id is not None and not isinstance(source.project_id, UUID))
    ):
        raise MoneyError("credit_integrity", "The original credit source is inconsistent.")
    values = (source.net, source.tax, source.total, source.credited_net, source.credited_tax)
    if (
        any(not isinstance(value, Amount) for value in values)
        or any(value.asset != source.total.asset or value.minor_units < 0 for value in values)
        or source.total.minor_units <= 0
        or source.net.minor_units + source.tax.minor_units != source.total.minor_units
        or source.credited_net > source.net
        or source.credited_tax > source.tax
    ):
        raise MoneyError("credit_integrity", "The original credit quantities are inconsistent.")
    new_net, new_tax = Amount.parse(net, source.total.asset), Amount.parse(tax, source.total.asset)
    if new_net.minor_units < 0 or new_tax.minor_units < 0:
        raise MoneyError("credit_amount", "Manual net and tax quantities cannot be negative.")
    remaining_net, remaining_tax = (
        source.net - source.credited_net,
        source.tax - source.credited_tax,
    )
    if new_net > remaining_net or new_tax > remaining_tax:
        raise MoneyError(
            "credit_exceeds_source", "Credit exceeds the original net or tax capacity."
        )
    total = new_net + new_tax
    if total.minor_units <= 0:
        raise MoneyError("credit_amount", "A credit must reduce a positive gross quantity.")
    invoice = source.document_kind == "invoice"
    category_amount = total if invoice else -total
    return CreditLinePlan(
        source.document_id,
        source.line_id,
        source.category_id,
        source.project_id,
        new_net,
        new_tax,
        total,
        remaining_net - new_net,
        remaining_tax - new_tax,
        "income" if invoice else "expense",
        category_amount,
        "receivable.customer" if invoice else "payable.supplier",
        -category_amount,
    )
