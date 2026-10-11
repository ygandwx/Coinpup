"""Pure validation of a reviewed draft and its per-line gross accrual amounts."""

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from coinpup_api.business.draft_schemas import BusinessDraftResponse
from coinpup_api.business.pricing import DocumentPrice, LinePrice, PriceInput, price_document
from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount


@dataclass(frozen=True, slots=True)
class AccrualLinePlan:
    line_id: UUID
    line_no: int
    category_id: UUID
    project_id: UUID | None
    recognition_date: date
    description: str
    price: LinePrice
    account_amount: Amount
    category_amount: Amount


@dataclass(frozen=True, slots=True)
class ConfirmationPlan:
    ledger_id: UUID
    document_id: UUID
    source_version: int
    party_id: UUID
    transaction_date: date
    operation_kind: str
    control_key: str
    category_kind: str
    price: DocumentPrice
    lines: tuple[AccrualLinePlan, ...]
    recognition_dates: tuple[date, ...]


def _integrity():
    return MoneyError("confirmation_integrity", "The reviewed draft is inconsistent.")


def plan_confirmation(draft: BusinessDraftResponse, asset: AssetDefinition) -> ConfirmationPlan:
    """Keep every date and zero-priced source; emit accruals only for positive lines."""
    if not isinstance(draft, BusinessDraftResponse) or draft.state != "draft":
        raise _integrity()
    if draft.archived:
        raise MoneyError("document_archived", "Restore the draft before confirming.")
    if (
        not isinstance(asset, AssetDefinition)
        or draft.document_kind not in {"invoice", "bill"}
        or draft.asset_id != asset.asset_id
    ):
        raise _integrity()
    category_kind = "income" if draft.document_kind == "invoice" else "expense"
    lines = sorted(draft.lines, key=lambda line: line.line_no)
    if (
        draft.line_count != len(lines)
        or len({line.id for line in lines}) != len(lines)
        or len({line.line_no for line in lines}) != len(lines)
    ):
        raise _integrity()
    for line in lines:
        if (
            line.archived
            or line.ledger_id != draft.ledger_id
            or line.document_id != draft.id
            or line.asset_id != asset.asset_id
            or line.category_kind != category_kind
            or type(line.line_no) is not int
            or line.line_no < 1
        ):
            raise _integrity()
    price = price_document(
        tuple(
            PriceInput(line.quantity, line.unit_price, line.discount_amount, line.tax_rate_percent)
            for line in lines
        ),
        asset,
    )
    for source, calculated in ((draft, price), *zip(lines, price.lines, strict=True)):
        if any(
            Amount.parse(getattr(source, name + "_amount"), asset) != getattr(calculated, name)
            for name in ("net", "tax", "total")
        ):
            raise _integrity()
    if price.total.minor_units <= 0:
        raise MoneyError("confirmation_total_positive", "Confirm a document with a positive total.")
    postings = []
    for line, calculated in zip(lines, price.lines, strict=True):
        if calculated.total.minor_units == 0:
            continue
        account = calculated.total if category_kind == "income" else -calculated.total
        postings.append(
            AccrualLinePlan(
                line.id,
                line.line_no,
                line.category_id,
                line.project_id,
                line.recognition_date,
                line.description,
                calculated,
                account,
                -account,
            )
        )
    return ConfirmationPlan(
        draft.ledger_id,
        draft.id,
        draft.version,
        draft.party_id,
        draft.issue_date,
        "invoice_accrual" if category_kind == "income" else "bill_accrual",
        "receivable.customer" if category_kind == "income" else "payable.supplier",
        category_kind,
        price,
        tuple(postings),
        tuple(sorted({line.recognition_date for line in lines})),
    )
