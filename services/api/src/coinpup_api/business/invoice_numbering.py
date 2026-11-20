"""Pure commercial number candidates, without allocating or promising a saved number."""

import re
import unicodedata
from dataclasses import dataclass
from typing import Annotated

from pydantic import ConfigDict, Field, StrictInt, StrictStr, field_validator

from coinpup_api.business.schemas import BusinessCommand

MAX_SEQUENCE = 2**63 - 1
SEQUENCE = re.compile(r"[1-9][0-9]{0,18}\Z")


class InvoiceNumberingPolicy(BusinessCommand):
    model_config = ConfigDict(extra="forbid", frozen=True)
    prefix: Annotated[StrictStr, Field(max_length=40)]
    minimum_digits: Annotated[StrictInt, Field(ge=1, le=20)]

    @field_validator("prefix")
    @classmethod
    def visible_prefix(cls, value):
        # Text only: never a date template, path, HTML or printf expression.
        if value != value.strip() or any(
            unicodedata.category(character).startswith("C")
            or (character.isspace() and character != " ")
            for character in value
        ):
            raise ValueError("Prefix requires visible text without surrounding whitespace")
        return value


class InvoiceNumberingError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class InvoiceNumberCandidate:
    sequence: str
    number: str
    next_sequence: str | None


def candidate_number(policy: InvoiceNumberingPolicy, sequence: str) -> InvoiceNumberCandidate:
    """The caller must separately lock, check historical uniqueness and reserve atomically."""
    if not isinstance(policy, InvoiceNumberingPolicy):
        raise InvoiceNumberingError("invalid_invoice_numbering_policy")
    # Do not trust an internally copied/constructed policy whose validation was bypassed.
    reviewed = InvoiceNumberingPolicy.model_validate(policy.model_dump())
    if not isinstance(sequence, str) or SEQUENCE.fullmatch(sequence) is None:
        raise InvoiceNumberingError("invalid_invoice_sequence")
    value = int(sequence)
    if value > MAX_SEQUENCE:
        raise InvoiceNumberingError("invoice_sequence_exhausted")
    return InvoiceNumberCandidate(
        sequence=sequence,
        number=reviewed.prefix + sequence.zfill(reviewed.minimum_digits),
        next_sequence=str(value + 1) if value < MAX_SEQUENCE else None,
    )
