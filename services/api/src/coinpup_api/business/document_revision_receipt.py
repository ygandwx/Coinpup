"""Immutable proof of the two ordered document versions and every actual accrual reversal."""

from dataclasses import replace
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from coinpup_api.business.credit_schemas import _receipt_quantity
from coinpup_api.business.document_revision_schemas import (
    ConfirmedDocumentCorrection,
    PriorDocumentVersion,
)
from coinpup_api.business.draft_schemas import BusinessDraftLineInput, DraftHeaderInput
from coinpup_api.business.pricing import MAX_DOCUMENT_LINES, price_document_rows
from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting_schemas import QuantityText
from coinpup_api.ledger.schemas import Version

StoredVersion = Annotated[Version, Field(le=2147483647)]


class DocumentAccrualPosting(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: UUID
    operation_version: StoredVersion
    journal_id: UUID


class DocumentReceiptLine(BusinessDraftLineInput):
    model_config = ConfigDict(extra="forbid", frozen=True)
    line_no: StoredVersion
    net_amount: QuantityText
    tax_amount: QuantityText
    total_amount: QuantityText
    posting: DocumentAccrualPosting | None
    _quantities = field_validator("net_amount", "tax_amount", "total_amount")(_receipt_quantity)

    @model_validator(mode="after")
    def posting_matches_total(self):
        if any(c in "123456789" for c in self.total_amount) != (self.posting is not None):
            raise ValueError("Only strictly positive accruals have a posting")
        return self


class DocumentReceiptGroup(DraftHeaderInput):
    model_config = ConfigDict(extra="forbid", frozen=True)
    lines: tuple[DocumentReceiptLine, ...] = Field(min_length=1, max_length=MAX_DOCUMENT_LINES)
    net_amount: QuantityText
    tax_amount: QuantityText
    total_amount: QuantityText
    _quantities = field_validator("net_amount", "tax_amount", "total_amount")(_receipt_quantity)

    @model_validator(mode="after")
    def distinct_ordered_membership(self):
        positions = [row.line_no for row in self.lines]
        if positions != sorted(set(positions)) or len({row.id for row in self.lines}) != len(
            self.lines
        ):
            raise ValueError("Each line identity and position occurs once in the sealed order")
        postings = [row.posting for row in self.lines if row.posting is not None]
        if not postings or len({p.operation_id for p in postings}) != len(postings):
            raise ValueError("Positive lines require distinct operations")
        return self

    def verify_prices(self, asset: AssetDefinition):
        if asset.asset_id != self.asset_id:
            raise MoneyError(
                "document_revision_integrity", "The saved asset identity is inconsistent."
            )
        historical = replace(asset, enabled=True)
        prices = price_document_rows(self.lines, historical)
        for fact, price in ((self, prices), *zip(self.lines, prices.lines, strict=True)):
            if any(
                Amount.from_decimal(Decimal(getattr(fact, field + "_amount")), historical)
                != getattr(price, field)
                for field in ("net", "tax", "total")
            ):
                raise MoneyError(
                    "document_revision_integrity", "The saved document amounts are inconsistent."
                )


class DocumentAccrualReversal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_line_id: UUID
    journal_id: UUID


class DocumentRevisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["business_document_revision"]
    ledger_id: UUID
    confirmation_id: UUID
    document_id: UUID
    revision_id: UUID
    previous_version: PriorDocumentVersion
    version: Annotated[StoredVersion, Field(ge=3)]
    action: Literal["correct", "cancel"]
    reason: Annotated[StrictStr, Field(min_length=1, max_length=1000)]
    previous: DocumentReceiptGroup
    replacement: DocumentReceiptGroup | None
    reversals: tuple[DocumentAccrualReversal, ...] = Field(
        min_length=1, max_length=MAX_DOCUMENT_LINES
    )

    @model_validator(mode="after")
    def prove_transition(self):
        if self.version != self.previous_version + 1 or (self.action == "correct") != (
            self.replacement is not None
        ):
            raise ValueError("The sealed action must advance the reviewed document exactly once")
        if self.reason != self.reason.strip() or "\x00" in self.reason:
            raise ValueError("The saved revision reason must be trimmed and contain no NUL")
        prior = {row.id: row.posting for row in self.previous.lines if row.posting is not None}
        if [v.source_line_id for v in self.reversals] != list(prior):
            raise ValueError("Every old positive line has one reversal in its previous order")
        if any(p.operation_version >= self.previous_version for p in prior.values()):
            raise ValueError("Old accrual versions must fit the number of document versions")
        journals = [p.journal_id for p in prior.values()] + [r.journal_id for r in self.reversals]
        if self.replacement is not None:
            rows = self.replacement.lines
            if self.replacement.document_kind != self.previous.document_kind or [
                r.line_no for r in rows
            ] != list(range(1, len(rows) + 1)):
                raise ValueError(
                    "Corrections preserve document kind and its complete new line order"
                )
            old_ids = {p.operation_id for p in prior.values()}
            for row in rows:
                if row.posting is None:
                    continue
                posting = row.posting
                old = prior.get(row.id)
                if old is None:
                    if posting.operation_id in old_ids or posting.operation_version != 1:
                        raise ValueError(
                            "New accruals start with a distinct operation at version one"
                        )
                elif (
                    posting.operation_id != old.operation_id
                    or posting.operation_version != old.operation_version + 1
                ):
                    raise ValueError("Each retained accrual advances its own operation once")
                journals.append(posting.journal_id)
        if len(set(journals)) != len(journals):
            raise ValueError("All prior, reversal and replacement journals must be distinct")
        return self


def verify_document_receipt_sources(receipt, current, command, postings, old_asset, new_asset):
    """Compare a candidate 200 with the actual sealed version, active journals and reviewed input.

    Replays can call this against the historical source and target facts. The database
    must separately prove the actual reversal/replacement journal contents at commit.
    """

    def mismatch():
        return MoneyError(
            "document_revision_integrity",
            "The receipt does not match its reviewed document sources.",
        )

    def same_amounts(fact, source):
        return all(
            Amount.from_decimal(Decimal(getattr(fact, name)), old_asset)
            == Amount.from_decimal(Decimal(getattr(source, name)), old_asset)
            for name in ("net_amount", "tax_amount", "total_amount")
        )

    correcting = isinstance(command, ConfirmedDocumentCorrection)
    if (
        receipt.document_id != current.id
        or receipt.ledger_id != current.ledger_id
        or receipt.confirmation_id != current.confirmation_id
        or receipt.previous_version != current.version
        or command.expected_version != current.version
        or receipt.reason != command.reason
        or (receipt.action == "correct") != correcting
        or (new_asset is not None) != correcting
    ):
        raise mismatch()
    headers = set(DraftHeaderInput.model_fields)
    source_inputs = set(BusinessDraftLineInput.model_fields)
    before = receipt.previous
    if (
        not same_amounts(before, current)
        or before.model_dump(include=headers) != current.model_dump(include=headers)
        or len(before.lines) != len(current.lines)
    ):
        raise mismatch()
    operations = {p.source_line_id: p for p in postings}
    if len(operations) != len(postings):
        raise mismatch()
    for fact, source in zip(before.lines, current.lines, strict=True):
        if (
            not same_amounts(fact, source)
            or fact.line_no != source.line_no
            or fact.model_dump(include=source_inputs) != source.model_dump(include=source_inputs)
        ):
            raise mismatch()
        stored = operations.pop(source.id, None)
        if (stored is None) != (fact.posting is None):
            raise mismatch()
        if stored and (
            stored.ledger_id != current.ledger_id
            or stored.document_id != current.id
            or (stored.operation_id, stored.version, stored.journal_id)
            != (fact.posting.operation_id, fact.posting.operation_version, fact.posting.journal_id)
        ):
            raise mismatch()
    if operations:
        raise mismatch()
    before.verify_prices(old_asset)
    if correcting:
        target = receipt.replacement
        proposed = command.replacement
        if (
            target.model_dump(include=headers) != proposed.model_dump(include=headers)
            or len(target.lines) != len(proposed.lines)
            or any(
                fact.model_dump(include=source_inputs) != row.model_dump()
                for fact, row in zip(target.lines, proposed.lines, strict=True)
            )
        ):
            raise mismatch()
        target.verify_prices(new_asset)
