"""Bounded OCR candidates and versioned jobs; these records never create postings."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.ledger.models import Versioned
from coinpup_api.models import Base


def _scope(table):
    return (
        ForeignKeyConstraint(
            ["ledger_id", "created_by"],
            ["ledgers.id", "ledgers.owner_id"],
            name=f"fk_{table}_ledger_owner",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "ledger_id", name=f"uq_{table}_id_ledger"),
        UniqueConstraint("id", "ledger_id", "created_by", name=f"uq_{table}_id_ledger_owner"),
        CheckConstraint("version > 0", name=f"ck_{table}_version"),
        CheckConstraint("updated_at >= created_at", name=f"ck_{table}_timestamps"),
    )


class OcrJob(Versioned, Base):
    __tablename__ = "ocr_jobs"
    __table_args__ = (
        *_scope("ocr_jobs"),
        ForeignKeyConstraint(
            ["file_id", "ledger_id"],
            ["stored_files.id", "stored_files.ledger_id"],
            name="fk_ocr_jobs_file_ledger",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("created_by", "intent_id", name="uq_ocr_jobs_owner_intent"),
        CheckConstraint("manifest_hash ~ '^[0-9a-f]{64}$'", name="ck_ocr_jobs_manifest_hash"),
        CheckConstraint("config_hash ~ '^[0-9a-f]{64}$'", name="ck_ocr_jobs_config_hash"),
        CheckConstraint(
            "jsonb_typeof(configuration) = 'object' AND octet_length(configuration::text) <= 16384",
            name="ck_ocr_jobs_configuration",
        ),
        CheckConstraint(
            "result IS NULL OR (jsonb_typeof(result) = 'object' "
            "AND octet_length(result::text) <= 1048576)",
            name="ck_ocr_jobs_result",
        ),
        CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_ocr_jobs_state",
        ),
        CheckConstraint("attempts BETWEEN 0 AND 3", name="ck_ocr_jobs_attempts"),
        CheckConstraint("generation >= 0", name="ck_ocr_jobs_generation"),
        CheckConstraint(
            "(state = 'running' AND lease_token IS NOT NULL AND lease_until IS NOT NULL "
            "AND attempts >= 1 AND generation >= 1) OR "
            "(state <> 'running' AND lease_token IS NULL AND lease_until IS NULL)",
            name="ck_ocr_jobs_lease",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,63}$'",
            name="ck_ocr_jobs_error_code",
        ),
        CheckConstraint(
            "(state <> 'succeeded' OR (result IS NOT NULL AND error_code IS NULL)) "
            "AND (state <> 'failed' OR error_code IS NOT NULL)",
            name="ck_ocr_jobs_completion",
        ),
        Index("ix_ocr_jobs_ready", "state", "retry_at"),
        Index("ix_ocr_jobs_ledger_id", "ledger_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    ledger_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    file_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    intent_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)


class OcrDraft(Versioned, Base):
    __tablename__ = "ocr_drafts"
    __table_args__ = (
        *_scope("ocr_drafts"),
        ForeignKeyConstraint(
            ["job_id", "ledger_id", "created_by"],
            ["ocr_jobs.id", "ocr_jobs.ledger_id", "ocr_jobs.created_by"],
            name="fk_ocr_drafts_job_ledger_owner",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("job_id", "source_key", name="uq_ocr_drafts_job_source"),
        CheckConstraint(
            "source_key ~ '[^[:space:]]' AND source_key !~ '[[:cntrl:]]'",
            name="ck_ocr_drafts_source_key",
        ),
        *(
            CheckConstraint(
                f"jsonb_typeof({column}) = 'object' AND octet_length({column}::text) <= 262144",
                name=f"ck_ocr_drafts_{column}",
            )
            for column in ("recognized", "evidence", "fields")
        ),
        CheckConstraint("status IN ('draft', 'ignored', 'confirmed')", name="ck_ocr_drafts_status"),
        Index("ix_ocr_drafts_ledger_id", "ledger_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    ledger_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_key: Mapped[str] = mapped_column(String(200), nullable=False)
    recognized: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    fields: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="draft")


class OcrConfirmation(Versioned, Base):
    __tablename__ = "ocr_confirmations"
    __table_args__ = (
        *_scope("ocr_confirmations"),
        ForeignKeyConstraint(
            ["draft_id", "ledger_id", "created_by"],
            ["ocr_drafts.id", "ocr_drafts.ledger_id", "ocr_drafts.created_by"],
            name="fk_ocr_confirmations_draft_ledger_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["target_ledger_id", "created_by"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_ocr_confirmations_target_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_file_id", "ledger_id"],
            ["stored_files.id", "stored_files.ledger_id"],
            name="fk_ocr_confirmations_source_file",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["target_file_id", "target_ledger_id"],
            ["stored_files.id", "stored_files.ledger_id"],
            name="fk_ocr_confirmations_target_file",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["operation_id", "target_ledger_id"],
            ["financial_operations.id", "financial_operations.ledger_id"],
            name="fk_ocr_confirmations_operation",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("draft_id", name="uq_ocr_confirmations_draft"),
        UniqueConstraint("ledger_id", "intent_id", name="uq_ocr_confirmations_ledger_intent"),
        CheckConstraint("hash_version = 2", name="ck_ocr_confirmations_hash_version"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="ck_ocr_confirmations_hash"),
        CheckConstraint(
            "draft_version > 0 AND operation_version > 0", name="ck_ocr_confirmations_versions"
        ),
        CheckConstraint("action IN ('create', 'link')", name="ck_ocr_confirmations_action"),
        *(
            CheckConstraint(
                f"jsonb_typeof({column}) = 'object' AND octet_length({column}::text) <= 262144",
                name=f"ck_ocr_confirmations_{column}",
            )
            for column in ("review", "response")
        ),
        Index("ix_ocr_confirmations_source", "ledger_id", "source_file_id", "source_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    ledger_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    draft_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    intent_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_file_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_key: Mapped[str] = mapped_column(String(200), nullable=False)
    target_ledger_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    target_file_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    draft_version: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_version: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    hash_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="2")
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    review: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
