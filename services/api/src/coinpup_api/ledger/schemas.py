"""Bounded structure commands shared by the HTTP API and domain service."""

import re
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    StrictInt,
    StrictStr,
    computed_field,
    field_validator,
    model_validator,
)

from coinpup_api.ledger.assets import verified_token

Name = Annotated[StrictStr, Field(min_length=1, max_length=160)]
AssetId = Annotated[StrictStr, Field(min_length=1, max_length=200)]
Version = Annotated[StrictInt, Field(ge=1)]
CardNumber = Annotated[SecretStr, Field(min_length=12, max_length=19)]
CategoryKind = Literal["income", "expense"]
AccountKind = Literal[
    "bank",
    "cash",
    "wechat",
    "alipay",
    "credit_card",
    "debit_card",
    "paypal",
    "wise",
    "stripe",
    "crypto",
]
CustodyMode = Literal["onchain", "custodial"]
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

    @field_validator("asset_ids", "crypto_preset_asset_ids", check_fields=False)
    @classmethod
    def distinct_assets(cls, value):
        if value is not None and len(value) != len(set(value)):
            raise ValueError("Account assets must be unique")
        return value

    @field_validator("crypto_assets", check_fields=False)
    @classmethod
    def distinct_crypto_assets(cls, value):
        if len({item.asset_id for item in value}) != len(value):
            raise ValueError("An asset may only have one profile change per request")
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
    code: Annotated[StrictStr, Field(min_length=2, max_length=10, pattern=r"^[A-Z0-9]+$")]
    kind: Literal["fiat", "native", "token", "custodial"]
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


class CryptoPlatformProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    institution_id: Annotated[StrictStr, Field(pattern=r"^[a-z0-9-]{2,40}$")] | None = None
    custom_name: Annotated[StrictStr, Field(min_length=1, max_length=80)] | None = None
    uid: Annotated[StrictStr, Field(min_length=1, max_length=160)] | None = None
    email: Annotated[StrictStr, Field(min_length=3, max_length=254)] | None = None
    note: Annotated[StrictStr, Field(min_length=1, max_length=1000)] | None = None

    @field_validator("custom_name", "uid", "email")
    @classmethod
    def clean_platform_text(cls, value):
        if value is None:
            return value
        value = value.strip()
        if not value or not value.isprintable():
            raise ValueError("Use printable profile text")
        return value

    @field_validator("note")
    @classmethod
    def clean_note(cls, value):
        if value is not None and (
            not value.strip()
            or any(not char.isprintable() and char not in "\n\r\t" for char in value)
        ):
            raise ValueError("Use printable notes")
        return value

    @model_validator(mode="after")
    def single_platform(self):
        if (self.institution_id is None) == (self.custom_name is None):
            raise ValueError("Select one catalog institution or enter a custom platform name")
        if self.email is not None and not re.fullmatch(
            r"[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+", self.email
        ):
            raise ValueError("Use a valid email address")
        return self


class CryptoAssetDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    network: Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")] | None = None
    address: Annotated[StrictStr, Field(min_length=1, max_length=128)] | None = None
    memo: Annotated[StrictStr, Field(min_length=1, max_length=256)] | None = None
    label: Annotated[StrictStr, Field(min_length=1, max_length=160)] | None = None

    @field_validator("memo", "label")
    @classmethod
    def printable_details(cls, value):
        if value is not None and (not value.strip() or not value.isprintable()):
            raise ValueError("Use printable asset details")
        return value


class CryptoAssetChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: AssetId
    details: CryptoAssetDetails | None


class CryptoAssetProfile(BaseModel):
    asset_id: AssetId
    enabled: bool
    details: CryptoAssetDetails


class AccountCreate(Command):
    card_number: CardNumber | None = None
    id: UUID | None = None
    name: Name
    kind: AccountKind
    custody_mode: CustodyMode | None = None
    details: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    asset_ids: Annotated[list[AssetId], Field(min_length=1, max_length=100)]
    crypto_platform: CryptoPlatformProfile | None = None
    crypto_assets: list[CryptoAssetChange] = Field(default_factory=list, max_length=100)
    crypto_preset_asset_ids: list[AssetId] = Field(default_factory=list, max_length=100)


class AccountUpdate(UpdateCommand):
    card_number: CardNumber | None = None
    name: Name | None = None
    kind: AccountKind | None = None
    custody_mode: CustodyMode | None = None
    details: dict[StrictStr, StrictStr] | None = None
    asset_ids: Annotated[list[AssetId], Field(min_length=1, max_length=100)] | None = None
    archived: StrictBool | None = None
    crypto_platform: CryptoPlatformProfile | None = None
    crypto_assets: list[CryptoAssetChange] = Field(default_factory=list, max_length=100)
    crypto_preset_asset_ids: list[AssetId] = Field(default_factory=list, max_length=100)


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

    @computed_field
    @property
    def verified(self) -> bool:
        """Whether a token matches the checked issuer contract, symbol and precision."""
        if self.kind != "token":
            return True
        official = verified_token(self.network, self.token_reference)
        return official is not None and (self.code, self.scale) == (official.code, official.scale)


class AssetPreset(BaseModel):
    asset_id: str
    code: str
    kind: Literal["native", "token", "custodial"]
    scale: int
    network: str | None
    token_reference: str | None
    verified_on: date
    sources: tuple[str, ...]


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
    custody_mode: CustodyMode | None = None
    details: dict[str, str]
    archived: bool
    asset_ids: list[str]
    has_card_number: bool = False
    crypto_platform: CryptoPlatformProfile | None = None
    crypto_assets: list[CryptoAssetProfile] = Field(default_factory=list)


class CardNumberResponse(BaseModel):
    card_number: str | None


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
