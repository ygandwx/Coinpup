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


class BusinessAllocationReturnRevision(SettlementIdentity, Base):
    __tablename__ = "business_return_revisions"
    __table_args__ = (
        *_identity("business_return_revision"),
        *_receipt("business_return_revision"),
        UniqueConstraint(
            "id", "return_id", "payment_id", "ledger_id", name="uq_business_return_revision_scope"
        ),
        UniqueConstraint(
            "return_id", "group_version", name="uq_business_return_revision_group_version"
        ),
        ForeignKeyConstraint(
            ["return_id", "payment_id", "ledger_id"],
            [
                "business_allocation_returns.id",
                "business_allocation_returns.payment_id",
                "business_allocation_returns.ledger_id",
            ],
            name="fk_business_return_revision_group",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["previous_revision_id", "return_id", "payment_id", "ledger_id"],
            [
                "business_return_revisions.id",
                "business_return_revisions.return_id",
                "business_return_revisions.payment_id",
                "business_return_revisions.ledger_id",
            ],
            name="fk_business_return_revision_previous",
            ondelete="RESTRICT",
        ),
        CheckConstraint("group_version >= 2", name="ck_business_return_revision_group_version"),
        CheckConstraint(
            "(group_version = 2 AND previous_revision_id IS NULL) OR "
            "(group_version > 2 AND previous_revision_id IS NOT NULL "
            "AND previous_revision_id <> id)",
            name="ck_business_return_revision_previous",
        ),
        CheckConstraint(
            "action IN ('correct', 'cancel')", name="ck_business_return_revision_action"
        ),
        CheckConstraint(
            "revision_reason ~ '^[^[:space:]]' AND revision_reason ~ '[^[:space:]]$'",
            name="ck_business_return_revision_reason",
        ),
        CheckConstraint(
            "(action = 'correct' AND return_date IS NOT NULL AND return_reason IS NOT NULL "
            "AND return_date BETWEEN DATE '0001-01-01' AND DATE '9999-12-31' "
            "AND return_reason ~ '^[^[:space:]]' AND return_reason ~ '[^[:space:]]$') OR "
            "(action = 'cancel' AND return_date IS NULL AND return_reason IS NULL)",
            name="ck_business_return_revision_replacement",
        ),
    )
    return_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    payment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    group_version: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_revision_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    action: Mapped[str] = mapped_column(String(7), nullable=False)
    revision_reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    return_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    return_reason: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_by: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
