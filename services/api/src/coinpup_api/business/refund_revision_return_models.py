"""Ordered immutable allocation-return evidence on a particular refund replacement."""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, Integer, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.business.settlement_models import SettlementIdentity, _identity
from coinpup_api.models import Base


class BusinessRefundRevisionReturn(SettlementIdentity, Base):
    __tablename__ = "business_refund_revision_returns"
    __table_args__ = (
        *_identity("refund_revision_return"),
        UniqueConstraint("revision_id", "line_no", name="uq_refund_revision_return_position"),
        UniqueConstraint("revision_id", "return_id", name="uq_refund_revision_return_source"),
        ForeignKeyConstraint(
            ["revision_id", "refund_id", "payment_id", "ledger_id"],
            [
                "business_refund_revisions.id",
                "business_refund_revisions.refund_id",
                "business_refund_revisions.payment_id",
                "business_refund_revisions.ledger_id",
            ],
            name="fk_refund_revision_return_parent",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["return_id", "payment_id", "ledger_id"],
            [
                "business_allocation_returns.id",
                "business_allocation_returns.payment_id",
                "business_allocation_returns.ledger_id",
            ],
            name="fk_refund_revision_return_source",
            ondelete="RESTRICT",
        ),
        CheckConstraint("return_version = 1", name="ck_refund_revision_return_source_version"),
        CheckConstraint("line_no BETWEEN 1 AND 200", name="ck_refund_revision_return_position"),
        Index("ix_refund_revision_returns_source", "ledger_id", "return_id"),
    )
    revision_id: Mapped[UUID] = mapped_column(Uuid)
    refund_id: Mapped[UUID] = mapped_column(Uuid)
    payment_id: Mapped[UUID] = mapped_column(Uuid)
    return_id: Mapped[UUID] = mapped_column(Uuid)
    return_version: Mapped[int] = mapped_column(Integer)
    line_no: Mapped[int] = mapped_column(Integer)
