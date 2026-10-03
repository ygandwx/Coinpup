"""Private document contracts. Content identities never contain filesystem paths."""

import unicodedata
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

MEDIA_TYPES = ("application/pdf", "image/jpeg", "image/png", "image/webp")
MediaType = Literal["application/pdf", "image/jpeg", "image/png", "image/webp"]
Version = Annotated[StrictInt, Field(ge=1)]


def _label(value: str) -> str:
    if not value.strip() or any(unicodedata.category(char) == "Cc" for char in value):
        raise ValueError("A nonempty label without control characters is required")
    return value


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class UploadCreate(InputModel):
    id: UUID
    original_filename: Annotated[str, Field(strict=True, min_length=1, max_length=255)]
    declared_size: Annotated[StrictInt, Field(ge=1, le=2**63 - 1)]
    operation_id: UUID | None = None

    @field_validator("original_filename")
    @classmethod
    def filename(cls, value):
        _label(value)
        if "/" in value or "\\" in value or value in {".", ".."}:
            raise ValueError("A filename without directory components is required")
        return value


class FileUpdate(InputModel):
    expected_version: Version
    title: Annotated[str, Field(strict=True, min_length=1, max_length=255)] | None = None
    archived: StrictBool | None = None

    @field_validator("title")
    @classmethod
    def title_label(cls, value):
        return _label(value) if value is not None else value

    @model_validator(mode="after")
    def nonempty_update(self):
        changed = self.model_fields_set - {"expected_version"}
        if not changed or any(getattr(self, key) is None for key in changed):
            raise ValueError("At least one non-null metadata change is required")
        return self


class LinkUpdate(InputModel):
    expected_version: Version
    archived: StrictBool


class UploadCompletion(BaseModel):
    upload_id: UUID
    file_id: UUID
    link_id: UUID | None
    duplicate: bool
    sha256: str
    byte_size: int
    media_type: MediaType


class UploadResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    ledger_id: UUID
    original_filename: str
    declared_size: int
    operation_id: UUID | None
    state: Literal["pending", "ready"]
    created_at: datetime
    completed_at: datetime | None
    response: UploadCompletion | None


class FileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    ledger_id: UUID
    created_by: UUID
    sha256: str
    byte_size: int
    detected_media_type: MediaType
    original_filename: str
    title: str
    archived: bool
    version: int
    created_at: datetime
    updated_at: datetime


class LinkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    ledger_id: UUID
    file_id: UUID
    operation_id: UUID
    created_by: UUID
    archived: bool
    version: int
    created_at: datetime
    updated_at: datetime


class FileConfiguration(BaseModel):
    max_upload_bytes: int
    upload_timeout_seconds: int
    supported_media_types: list[MediaType]
