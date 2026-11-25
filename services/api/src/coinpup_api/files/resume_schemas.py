"""Byte-offset progress for the existing privately authorized upload reservation."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StrictStr

from coinpup_api.files.schemas import InputModel


class UploadResumeCreate(InputModel):
    sha256: Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{64}$")]


class UploadResumeStatus(BaseModel):
    upload_id: UUID
    ledger_id: UUID
    device_id: UUID
    sha256: str
    declared_size: int
    next_offset: int
    max_chunk_bytes: int
    state: Literal["pending", "ready", "abandoned"]
