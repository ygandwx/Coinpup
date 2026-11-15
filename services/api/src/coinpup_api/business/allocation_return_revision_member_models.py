"""Complete transition members; the revision header still rejects every financial write."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.business.settlement_models import SettlementIdentity, _identity
from coinpup_api.models import Base


class BusinessReturnRevisionMember(SettlementIdentity, Base):
    __tablename__ = "business_return_revision_members"
    __table_args__ = (
        *_identity("return_revision_member"),
        *(
            UniqueConstraint("revision_id", column, name=f"uq_return_revision_member_{suffix}")
            for column, suffix in (
                ("member_no", "position"),
                ("source_allocation_line_id", "source"),
                ("operation_id", "operation"),
                ("previous_line_no", "previous_position"),
                ("line_no", "next_position"),
            )
        ),
        UniqueConstraint(
            "operation_id", "operation_version", name="uq_return_revision_member_version"
        ),
        UniqueConstraint("journal_id", name="uq_return_revision_member_journal"),
        UniqueConstraint("reversal_journal_id", name="uq_return_revision_member_reversal"),
        ForeignKeyConstraint(
            ["revision_id", "return_id", "payment_id", "ledger_id"],
            [
                f"business_return_revisions.{column}"
                for column in ("id", "return_id", "payment_id", "ledger_id")
            ],
            name="fk_return_revision_member_group",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_allocation_line_id", "payment_id", "source_operation_id", "ledger_id"],
            [
                f"business_allocation_lines.{column}"
                for column in ("id", "payment_id", "operation_id", "ledger_id")
            ],
            name="fk_return_revision_member_source",
            ondelete="RESTRICT",
        ),
        *(
            ForeignKeyConstraint(
                [column, "ledger_id"],
                ["financial_operations.id", "financial_operations.ledger_id"],
                name=f"fk_return_revision_member_{suffix}",
                ondelete="RESTRICT",
            )
            for column, suffix in (
                ("operation_id", "operation"),
                ("source_predecessor_operation_id", "source_predecessor"),
            )
        ),
        *(
            ForeignKeyConstraint(
                [column, "operation_id", "ledger_id"],
                ["journals.id", "journals.operation_id", "journals.ledger_id"],
                name=f"fk_return_revision_member_{suffix}",
                deferrable=True,
                initially="DEFERRED",
            )
            for column, suffix in (
                ("previous_journal_id", "previous"),
                ("reversal_journal_id", "reversal"),
                ("journal_id", "journal"),
            )
        ),
        Index(
            "ix_return_revision_member_source_order",
            "ledger_id",
            "source_allocation_line_id",
            "source_return_no",
        ),
        CheckConstraint(
            "source_version = 1 AND source_return_no >= 1",
            name="ck_return_revision_member_source_order",
        ),
        CheckConstraint(
            "(source_return_no = 1 AND source_predecessor_operation_id IS NULL) OR "
            "(source_return_no > 1 AND source_predecessor_operation_id IS NOT NULL "
            "AND source_predecessor_operation_id <> operation_id "
            "AND source_predecessor_operation_id <> source_operation_id)",
            name="ck_return_revision_member_source_predecessor",
        ),
        CheckConstraint(
            "source_operation_id <> operation_id",
            name="ck_return_revision_member_distinct_operation",
        ),
        CheckConstraint(
            "prefix_payment_amount >= 0 AND prefix_payment_amount < 100000000000000000000 "
            "AND prefix_document_amount >= 0 AND prefix_document_amount < 100000000000000000000 "
            "AND (prefix_payment_amount = 0) = (prefix_document_amount = 0) "
            "AND (source_return_no > 1 OR prefix_payment_amount = 0)",
            name="ck_return_revision_member_prefix",
        ),
        CheckConstraint("member_no BETWEEN 1 AND 400", name="ck_return_revision_member_position"),
        CheckConstraint(
            "action IN ('create', 'correct', 'cancel')", name="ck_return_revision_member_action"
        ),
        CheckConstraint(
            "(action = 'create' AND previous_line_no IS NULL "
            "AND previous_operation_version IS NULL "
            "AND previous_journal_id IS NULL AND reversal_journal_id IS NULL "
            "AND operation_version = 1) "
            "OR (action IN ('correct', 'cancel') AND previous_line_no IS NOT NULL "
            "AND previous_line_no BETWEEN 1 AND 200 AND previous_operation_version IS NOT NULL "
            "AND previous_operation_version BETWEEN 1 AND 2147483646 "
            "AND operation_version::bigint = previous_operation_version::bigint + 1 "
            "AND previous_journal_id IS NOT NULL AND reversal_journal_id IS NOT NULL "
            "AND reversal_journal_id <> previous_journal_id)",
            name="ck_return_revision_member_previous",
        ),
        CheckConstraint(
            "(action IN ('create', 'correct') AND line_no IS NOT NULL "
            "AND line_no BETWEEN 1 AND 200 "
            "AND journal_id IS NOT NULL AND payment_amount IS NOT NULL "
            "AND document_amount IS NOT NULL "
            "AND payment_amount > 0 AND payment_amount < 100000000000000000000 "
            "AND document_amount > 0 AND document_amount < 100000000000000000000 "
            "AND absorbs_remainder IS NOT NULL "
            "AND (action = 'create' OR (journal_id <> previous_journal_id "
            "AND journal_id <> reversal_journal_id))) OR (action = 'cancel' AND line_no IS NULL "
            "AND journal_id IS NULL AND payment_amount IS NULL AND document_amount IS NULL "
            "AND absorbs_remainder IS NULL)",
            name="ck_return_revision_member_replacement",
        ),
    )
    revision_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    return_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    payment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_allocation_line_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_operation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    member_no: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(7), nullable=False)
    operation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    operation_version: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_line_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_operation_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_journal_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    reversal_journal_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    line_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    journal_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_return_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_predecessor_operation_id: Mapped[UUID | None] = mapped_column(Uuid)
    prefix_payment_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    prefix_document_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    payment_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    document_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    absorbs_remainder: Mapped[bool | None] = mapped_column(Boolean)
