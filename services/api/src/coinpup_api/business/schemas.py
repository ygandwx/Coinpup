"""Bounded master data commands; stable identities do not constitute financial receipts."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictStr, field_validator, model_validator

from coinpup_api.ledger.schemas import Command, Name, RecordResponse, UpdateCommand

PartyRole = Literal["customer", "supplier", "both"]
Notes = Annotated[StrictStr, Field(max_length=2000)]


class BusinessCommand(Command):
    @field_validator("notes", "email", "phone", "address", "tax_identifier", check_fields=False)
    @classmethod
    def no_nul(cls, value):
        if value is not None and "\x00" in value:
            raise ValueError("Text cannot contain NUL characters")
        return value


class PartyFields(BusinessCommand):
    legal_name: Annotated[StrictStr, Field(max_length=200)] | None = None
    email: Annotated[StrictStr, Field(max_length=254)] | None = None
    phone: Annotated[StrictStr, Field(max_length=64)] | None = None
    address: Annotated[StrictStr, Field(max_length=1000)] | None = None
    tax_identifier: Annotated[StrictStr, Field(max_length=128)] | None = None
    notes: Notes | None = None


class PartyCreate(PartyFields):
    id: UUID
    name: Name
    role: PartyRole


class PartyUpdate(PartyFields, UpdateCommand):
    name: Name | None = None
    role: PartyRole | None = None
    archived: StrictBool | None = None

    @model_validator(mode="after")
    def non_null_role(self):
        if "role" in self.model_fields_set and self.role is None:
            raise ValueError("role cannot be null")
        return self


class ProjectCreate(BusinessCommand):
    id: UUID
    name: Name
    notes: Notes | None = None


class ProjectUpdate(BusinessCommand, UpdateCommand):
    name: Name | None = None
    notes: Notes | None = None
    archived: StrictBool | None = None


class BusinessIdentityResponse(RecordResponse):
    id: UUID
    ledger_id: UUID
    archived: bool


class PartyResponse(BusinessIdentityResponse):
    name: str | None
    role: PartyRole | None
    legal_name: str | None
    email: str | None
    phone: str | None
    address: str | None
    tax_identifier: str | None
    notes: str | None


class ProjectResponse(BusinessIdentityResponse):
    name: str
    notes: str | None
