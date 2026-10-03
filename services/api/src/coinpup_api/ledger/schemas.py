"""Bounded structure commands shared by the HTTP API and domain service."""

import re
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

Name = Annotated[StrictStr, Field(min_length=1, max_length=160)]
AssetId = Annotated[StrictStr, Field(min_length=1, max_length=200)]
Version = Annotated[StrictInt, Field(ge=1)]
CategoryKind = Literal["income", "expense"]
AccountKind = Literal[
    "bank", "cash", "wechat", "alipay", "credit_card", "paypal", "wise", "stripe", "crypto"
]
TemplateKey = Literal["personal_default", "business_default"]


def validate_details(value: dict[str, str]) -> dict[str, str]:
    if len(value) > 32:
        raise ValueError("At most 32 detail fields are allowed")
    for key, item in value.items():
        if not 1 <= len(key) <= 64 or not key.strip() or len(item) > 2000:
            raise ValueError("Detail keys and values exceed their bounds")
        if any(ord(char) < 32 for char in key) or "\x00" in item:
            raise ValueError("Detail fields contain invalid control characters")
    return value


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("name", "name_en", "legal_name", check_fields=False)
    @classmethod
    def clean_name(cls, value):
        if value is None:
            return value
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("A non-empty name without control characters is required")
        return value

    @field_validator("details", check_fields=False)
    @classmethod
    def bounded_details(cls, value):
        return validate_details(value) if value is not None else value

    @field_validator("asset_ids", check_fields=False)
    @classmethod
    def distinct_assets(cls, value):
        if value is not None and len(value) != len(set(value)):
            raise ValueError("Account assets must be unique")
        return value

    @field_validator("registration_date", mode="before", check_fields=False)
    @classmethod
    def date_without_time_or_numeric_coercion(cls, value):
        if value is None or type(value) is date:
            return value
        if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return value
        raise ValueError("Use a calendar date in YYYY-MM-DD format")


class UpdateCommand(Command):
    expected_version: Version

    @model_validator(mode="after")
    def reject_empty_update_and_null_required(self):
        changes = self.model_fields_set - {"expected_version"}
        if not changes:
            raise ValueError("At least one changed field is required")
        for field in changes & {"name", "details", "archived", "enabled", "asset_ids", "kind"}:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class AssetCreate(Command):
    code: Annotated[StrictStr, Field(min_length=3, max_length=4)]
    kind: Literal["fiat", "native", "token"]
    scale: Annotated[StrictInt, Field(ge=0, le=18)]
    network: Annotated[StrictStr, Field(max_length=64)] | None = None
    token_reference: Annotated[StrictStr, Field(max_length=128)] | None = None
    enabled: StrictBool = True


class AssetUpdate(UpdateCommand):
    enabled: StrictBool


class EntityProfile(Command):
    kind: Literal["personal", "company"]
    name: Name
    legal_name: Name | None = None
    country_code: Literal["CN", "HK", "US", "EE"] | None = None
    region_code: Annotated[StrictStr, Field(max_length=32)] | None = None
    company_type: Annotated[StrictStr, Field(max_length=64)] | None = None
    registration_date: date | None = None
    details: dict[StrictStr, StrictStr] = Field(default_factory=dict)

    @model_validator(mode="after")
    def company_profile(self):
        if self.kind == "personal":
            if any(
                value is not None
                for value in (
                    self.legal_name,
                    self.country_code,
                    self.region_code,
                    self.company_type,
                    self.registration_date,
                )
            ):
                raise ValueError("Company profile fields do not belong to personal entities")
            return self
        profiles = {
            "CN": "limited_liability",
            "HK": "private_limited",
            "US": "llc",
            "EE": "private_limited",
        }
        if self.country_code is None or self.company_type != profiles[self.country_code]:
            raise ValueError("Select a supported country and company type")
        if self.country_code == "US":
            if self.region_code not in {"NM", "WY"}:
                raise ValueError("Select NM or WY for the configured US LLC profile")
        elif self.region_code is not None:
            raise ValueError("This company profile does not use a region code")
        return self


class EntityCreate(EntityProfile):
    id: UUID | None = None
    ledger_id: UUID | None = None
    base_asset_id: AssetId
    template_key: TemplateKey | None = None
    locale: Literal["zh", "en"] = "zh"


class EntityUpdate(UpdateCommand):
    name: Name | None = None
    legal_name: Name | None = None
    country_code: Literal["CN", "HK", "US", "EE"] | None = None
    region_code: Annotated[StrictStr, Field(max_length=32)] | None = None
    company_type: Annotated[StrictStr, Field(max_length=64)] | None = None
    registration_date: date | None = None
    details: dict[StrictStr, StrictStr] | None = None
    archived: StrictBool | None = None


class AccountCreate(Command):
    id: UUID | None = None
    name: Name
    kind: AccountKind
    details: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    asset_ids: Annotated[list[AssetId], Field(min_length=1, max_length=100)]


class AccountUpdate(UpdateCommand):
    name: Name | None = None
    kind: AccountKind | None = None
    details: dict[StrictStr, StrictStr] | None = None
    asset_ids: Annotated[list[AssetId], Field(min_length=1, max_length=100)] | None = None
    archived: StrictBool | None = None


class CategoryCreate(Command):
    id: UUID | None = None
    name: Name
    name_en: Name | None = None
    kind: CategoryKind
    parent_id: UUID | None = None


class CategoryUpdate(UpdateCommand):
    name: Name | None = None
    name_en: Name | None = None
    archived: StrictBool | None = None


class RecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    version: int
    created_at: datetime
    updated_at: datetime


class AssetResponse(RecordResponse):
    asset_id: str
    code: str
    kind: str
    scale: int
    network: str | None
    token_reference: str | None
    enabled: bool


class LedgerResponse(RecordResponse):
    id: UUID
    entity_id: UUID
    base_asset_id: str


class EntityResponse(RecordResponse):
    id: UUID
    kind: str
    name: str
    legal_name: str | None
    country_code: str | None
    region_code: str | None
    company_type: str | None
    registration_date: date | None
    details: dict[str, str]
    archived: bool
    ledger: LedgerResponse


class AccountResponse(RecordResponse):
    id: UUID
    ledger_id: UUID
    name: str
    kind: str
    details: dict[str, str]
    archived: bool
    asset_ids: list[str]


class CategoryResponse(RecordResponse):
    id: UUID
    ledger_id: UUID
    name: str
    name_en: str | None
    kind: str
    parent_id: UUID | None
    template_key: str | None
    archived: bool


class TemplateCategory(BaseModel):
    model_config = ConfigDict(frozen=True)
    key: str
    name: str
    name_en: str
    kind: CategoryKind


class TemplateResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    key: TemplateKey
    name: str
    name_en: str
    categories: tuple[TemplateCategory, ...]
