"""Append-only revision headers; dedicated group validation still gates every commit."""

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
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.business.settlement_models import SettlementIdentity, _identity, _receipt
from coinpup_api.models import Base


class BusinessCreditRevision(SettlementIdentity, Base):
    __tablename__ = "business_credit_revisions"
    __table_args__ = (
        *_identity("business_credit_revision"),
        *_receipt("business_credit_revision"),
        UniqueConstraint(
            "id", "credit_id", "document_id", "ledger_id", name="uq_business_credit_revision_scope"
        ),
        UniqueConstraint(
            "credit_id", "group_version", name="uq_business_credit_revision_group_version"
        ),
        ForeignKeyConstraint(
            ["credit_id", "document_id", "ledger_id"],
            ["business_credits.id", "business_credits.document_id", "business_credits.ledger_id"],
            name="fk_business_credit_revision_group",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["previous_revision_id", "credit_id", "document_id", "ledger_id"],
            [
                "business_credit_revisions.id",
                "business_credit_revisions.credit_id",
                "business_credit_revisions.document_id",
                "business_credit_revisions.ledger_id",
            ],
            name="fk_business_credit_revision_previous",
            ondelete="RESTRICT",
        ),
        CheckConstraint("group_version >= 2", name="ck_business_credit_revision_group_version"),
        CheckConstraint(
            "(group_version = 2 AND previous_revision_id IS NULL) OR "
            "(group_version > 2 AND previous_revision_id IS NOT NULL "
            "AND previous_revision_id <> id)",
            name="ck_business_credit_revision_previous",
        ),
        CheckConstraint(
            "action IN ('correct', 'cancel')", name="ck_business_credit_revision_action"
        ),
        CheckConstraint(
            "revision_reason = btrim(revision_reason) AND revision_reason ~ '[^[:space:]]'",
            name="ck_business_credit_revision_reason",
        ),
        CheckConstraint(
            "(action = 'correct' AND credit_date IS NOT NULL AND credit_reason IS NOT NULL "
            "AND credit_date BETWEEN DATE '0001-01-01' AND DATE '9999-12-31' "
            "AND btrim(credit_reason) <> '') OR "
            "(action = 'cancel' AND credit_date IS NULL AND credit_reason IS NULL)",
            name="ck_business_credit_revision_replacement",
        ),
    )
    credit_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    document_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    group_version: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_revision_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    action: Mapped[str] = mapped_column(String(7), nullable=False)
    revision_reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    credit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    credit_reason: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_by: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
