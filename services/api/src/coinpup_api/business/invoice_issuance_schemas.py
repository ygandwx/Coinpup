"""Pure reviewed configuration/issuance inputs; no write route or numbering allocation."""

from uuid import UUID

from pydantic import StrictBool, model_validator

from coinpup_api.business.invoice_numbering import InvoiceNumberingPolicy
from coinpup_api.business.schemas import BusinessCommand, Notes
from coinpup_api.ledger.schemas import Name, Version


class InvoiceProfileFields(BusinessCommand):
    name: Name
    numbering: InvoiceNumberingPolicy
    issuer_extra_text: Notes | None = None
    payment_instructions: Notes | None = None
    footer: Notes | None = None
    logo_file_id: UUID | None = None
    logo_file_version: Version | None = None

    @model_validator(mode="after")
    def reviewed_logo_and_text(self):
        if (self.logo_file_id is None) != (self.logo_file_version is None):
            raise ValueError("Logo review requires both file identity and version")
        if any(
            value is not None and "\x00" in value
            for value in (self.issuer_extra_text, self.payment_instructions, self.footer)
        ):
            raise ValueError("Invoice display text cannot contain NUL")
        return self


class InvoiceProfileCreate(InvoiceProfileFields):
    id: UUID


class InvoiceProfileReplacement(InvoiceProfileFields):
    expected_version: Version


class InvoiceProfileArchive(BusinessCommand):
    expected_version: Version
    archived: StrictBool


class InvoiceIssuanceCreate(BusinessCommand):
    id: UUID
    document_id: UUID
    expected_document_version: Version
    configuration_id: UUID
    expected_configuration_version: Version
    previous_issuance_id: UUID | None

    @model_validator(mode="after")
    def not_own_predecessor(self):
        if self.previous_issuance_id == self.id:
            raise ValueError("An issuance cannot replace itself")
        return self
