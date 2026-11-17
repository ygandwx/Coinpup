"""Sealed confirmed-document revision headers and their reviewed replacement party."""

from datetime import date
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.business.settlement_models import SettlementIdentity, _identity, _receipt
from coinpup_api.models import Base


class BusinessDocumentRevision(SettlementIdentity, Base):
    __tablename__ = "business_document_revisions"
    __table_args__ = (
        *_identity("business_document_revision"),
        *_receipt("business_document_revision"),
        UniqueConstraint("document_id", "document_version", name="uq_document_revision_version"),
        UniqueConstraint(
            "id",
            "confirmation_id",
            "document_id",
            "ledger_id",
            "document_version",
            name="uq_document_revision_version_scope",
        ),
        ForeignKeyConstraint(
            ["confirmation_id", "document_id", "ledger_id"],
            [
                "business_confirmations.id",
                "business_confirmations.document_id",
                "business_confirmations.ledger_id",
            ],
            name="fk_document_revision_confirmation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "previous_revision_id",
                "confirmation_id",
                "document_id",
                "ledger_id",
                "previous_version",
            ],
            [
                "business_document_revisions.id",
                "business_document_revisions.confirmation_id",
                "business_document_revisions.document_id",
                "business_document_revisions.ledger_id",
                "business_document_revisions.document_version",
            ],
            name="fk_document_revision_previous",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["party_id", "ledger_id"],
            ["business_parties.id", "business_parties.ledger_id"],
            name="fk_document_revision_party",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["asset_id"],
            ["assets.asset_id"],
            name="fk_document_revision_asset",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "previous_version BETWEEN 2 AND 2147483646 AND "
            "document_version - 1::bigint = previous_version",
            name="ck_document_revision_versions",
        ),
        CheckConstraint(
            "previous_revision_id IS NULL OR previous_revision_id <> id",
            name="ck_document_revision_previous",
        ),
        CheckConstraint("action IN ('correct', 'cancel')", name="ck_document_revision_action"),
        CheckConstraint(
            "reason ~ '^[^[:space:]]' AND reason ~ '[^[:space:]]$'",
            name="ck_document_revision_reason",
        ),
        CheckConstraint(
            "(action = 'correct' AND party_id IS NOT NULL AND asset_id IS NOT NULL "
            "AND issue_date IS NOT NULL "
            "AND issue_date BETWEEN DATE '0001-01-01' AND DATE '9999-12-31' "
            "AND (due_date IS NULL OR due_date BETWEEN DATE '0001-01-01' AND DATE '9999-12-31') "
            "AND party_snapshot IS NOT NULL AND jsonb_typeof(party_snapshot) = 'object') OR "
            "(action = 'cancel' AND party_id IS NULL AND asset_id IS NULL AND issue_date IS NULL "
            "AND due_date IS NULL AND notes IS NULL AND party_snapshot IS NULL)",
            name="ck_document_revision_replacement",
        ),
    )
    confirmation_id: Mapped[UUID] = mapped_column(Uuid)
    document_id: Mapped[UUID] = mapped_column(Uuid)
    previous_version: Mapped[int] = mapped_column(Integer)
    document_version: Mapped[int] = mapped_column(Integer)
    previous_revision_id: Mapped[UUID | None] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(7))
    reason: Mapped[str] = mapped_column(String(1000))
    party_id: Mapped[UUID | None] = mapped_column(Uuid)
    asset_id: Mapped[str | None] = mapped_column(String(200))
    issue_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(String(2000))
    party_snapshot: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    created_by: Mapped[UUID] = mapped_column(Uuid)
    idempotency_key: Mapped[str] = mapped_column(String(128))
