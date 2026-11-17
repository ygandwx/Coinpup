"""Whole confirmed-document inputs. The original confirmation and its 201 stay immutable."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from coinpup_api.business.draft_schemas import BusinessDraftLineInput, DraftInput
from coinpup_api.business.pricing import MAX_DOCUMENT_LINES, DocumentPrice, price_document_rows
from coinpup_api.business.schemas import BusinessCommand
from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.posting_schemas import RevisionCommand
from coinpup_api.ledger.schemas import Version

PriorDocumentVersion = Annotated[Version, Field(ge=2, le=2147483646)]


class ReviewedDocumentReference(BusinessCommand):
    kind: Literal["party", "category", "project"]
    id: UUID
    version: Annotated[Version, Field(le=2147483647)]


class ConfirmedDocumentReplacement(DraftInput):
    lines: list[BusinessDraftLineInput] = Field(min_length=1, max_length=MAX_DOCUMENT_LINES)
    # Existing snapshots can be retained. A new or explicitly refreshed reference must
    # be compared with the reviewed master-data version under the write lock.
    reviewed_references: list[ReviewedDocumentReference] = Field(
        default_factory=list, max_length=1 + 2 * MAX_DOCUMENT_LINES
    )

    @model_validator(mode="after")
    def selected_references(self):
        allowed = {("party", self.party_id)}
        allowed.update(("category", line.category_id) for line in self.lines)
        allowed.update(("project", line.project_id) for line in self.lines if line.project_id)
        actual = [(item.kind, item.id) for item in self.reviewed_references]
        if len(set(actual)) != len(actual) or not set(actual) <= allowed:
            raise ValueError("Reviewed references must be distinct and present in the replacement")
        return self

    def price(self, document_kind: str, asset: AssetDefinition) -> DocumentPrice:
        if self.document_kind != document_kind or self.asset_id != asset.asset_id:
            raise MoneyError(
                "document_revision_scope", "The document kind or asset is inconsistent."
            )
        price = price_document_rows(self.lines, asset)
        if price.total.minor_units <= 0:
            raise MoneyError(
                "confirmation_total_positive", "Use a replacement with a positive total."
            )
        return price


class ConfirmedDocumentCorrection(RevisionCommand):
    expected_version: PriorDocumentVersion
    replacement: ConfirmedDocumentReplacement


class ConfirmedDocumentCancellation(RevisionCommand):
    expected_version: PriorDocumentVersion
