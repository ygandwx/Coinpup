"""Fixed issuance evidence, distinct from a reservation that still needs both PDFs.

These are pure contracts. Validation does not allocate a number, persist files,
authorize a ledger or prove that a database transaction has committed.
"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from coinpup_api.business.invoice_numbering import InvoiceNumberingPolicy, candidate_number
from coinpup_api.ledger.schemas import Version

Sha256 = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]
StoredVersion = Annotated[Version, Field(le=2147483647)]


class FrozenIssuanceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class InvoicePdfArtifact(FrozenIssuanceEvidence):
    language: Literal["zh", "en"]
    file_id: UUID
    sha256: Sha256
    byte_size: Annotated[StrictInt, Field(ge=1, le=2**63 - 1)]
    media_type: Literal["application/pdf"]


class InvoiceIssuanceIdentity(FrozenIssuanceEvidence):
    kind: Literal["invoice_issuance"]
    id: UUID
    ledger_id: UUID
    document_id: UUID
    document_version: Annotated[StoredVersion, Field(ge=2)]
    configuration_id: UUID
    configuration_version: StoredVersion
    previous_issuance_id: UUID | None
    numbering: InvoiceNumberingPolicy
    sequence: Annotated[StrictStr, Field(min_length=1, max_length=19)]
    number: Annotated[StrictStr, Field(min_length=1, max_length=60)]
    snapshot_sha256: Sha256
    template_version: Annotated[StrictStr, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]

    @model_validator(mode="after")
    def fixed_number_and_predecessor(self):
        if self.previous_issuance_id == self.id:
            raise ValueError("An issuance cannot replace itself")
        if self.number != candidate_number(self.numbering, self.sequence).number:
            raise ValueError("The reserved number must match its exact policy and sequence")
        return self


class InvoiceIssuancePending(InvoiceIssuanceIdentity):
    state: Literal["pending"]
    # A single rendered PDF must never be mistaken for a completed issuance.
    artifacts: tuple[InvoicePdfArtifact, ...] = Field(min_length=0, max_length=0)


class InvoiceIssuanceComplete(InvoiceIssuanceIdentity):
    state: Literal["complete"]
    artifacts: tuple[InvoicePdfArtifact, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def both_languages_once(self):
        if tuple(item.language for item in self.artifacts) != ("zh", "en"):
            raise ValueError("A complete issuance has the fixed Chinese and English pair")
        if self.artifacts[0].file_id == self.artifacts[1].file_id:
            raise ValueError("The two language artifacts have distinct private file identities")
        return self


InvoiceIssuanceResult = Annotated[
    InvoiceIssuancePending | InvoiceIssuanceComplete, Field(discriminator="state")
]


def verify_issuance_result_identity(result, reservation: InvoiceIssuanceIdentity):
    """Check against a saved reservation, never against mutable current configuration.

    The caller must first authorize its ledger and verify the frozen snapshot and
    actual stored file bytes separately. Revalidate to catch model_construct/copy
    bypasses, and return an independently frozen result rather than trusting them.
    """
    from pydantic import TypeAdapter

    expected = InvoiceIssuanceIdentity.model_validate(reservation.model_dump())
    observed = TypeAdapter(InvoiceIssuanceResult).validate_python(
        result.model_dump() if isinstance(result, BaseModel) else result
    )
    identity = observed.model_dump(include=set(InvoiceIssuanceIdentity.model_fields))
    if identity != expected.model_dump():
        raise ValueError("The issuance result does not match its saved reservation")
    return observed
