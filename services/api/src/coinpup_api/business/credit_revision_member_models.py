"""Complete transition members; the revision header still rejects every financial write."""

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.business.settlement_models import SettlementIdentity, _identity
from coinpup_api.models import Base


class BusinessCreditRevisionMember(SettlementIdentity, Base):
    __tablename__ = "business_credit_revision_members"
    __table_args__ = (
        *_identity("credit_revision_member"),
        *(
            UniqueConstraint("revision_id", column, name=f"uq_credit_revision_member_{suffix}")
            for column, suffix in (
                ("member_no", "position"),
                ("source_line_id", "source"),
                ("operation_id", "operation"),
                ("previous_line_no", "previous_position"),
                ("line_no", "next_position"),
            )
        ),
        UniqueConstraint(
            "operation_id", "operation_version", name="uq_credit_revision_member_version"
        ),
        UniqueConstraint("journal_id", name="uq_credit_revision_member_journal"),
        UniqueConstraint("reversal_journal_id", name="uq_credit_revision_member_reversal"),
        ForeignKeyConstraint(
            ["revision_id", "credit_id", "document_id", "ledger_id"],
            [
                f"business_credit_revisions.{column}"
                for column in ("id", "credit_id", "document_id", "ledger_id")
            ],
            name="fk_credit_revision_member_group",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_line_id", "document_id", "ledger_id"],
            [f"business_document_lines.{column}" for column in ("id", "document_id", "ledger_id")],
            name="fk_credit_revision_member_source",
            ondelete="RESTRICT",
        ),
        *(
            ForeignKeyConstraint(
                [column, "ledger_id"],
                ["financial_operations.id", "financial_operations.ledger_id"],
                name=f"fk_credit_revision_member_{suffix}",
                ondelete="RESTRICT",
            )
            for column, suffix in (
                ("operation_id", "operation"),
                ("source_operation_id", "original"),
            )
        ),
        *(
            ForeignKeyConstraint(
                [column, "operation_id", "ledger_id"],
                ["journals.id", "journals.operation_id", "journals.ledger_id"],
                name=f"fk_credit_revision_member_{suffix}",
                deferrable=True,
                initially="DEFERRED",
            )
            for column, suffix in (
                ("previous_journal_id", "previous"),
                ("reversal_journal_id", "reversal"),
                ("journal_id", "journal"),
            )
        ),
        CheckConstraint("member_no BETWEEN 1 AND 400", name="ck_credit_revision_member_position"),
        CheckConstraint(
            "action IN ('create', 'correct', 'cancel')", name="ck_credit_revision_member_action"
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
            name="ck_credit_revision_member_previous",
        ),
        CheckConstraint(
            "(action IN ('create', 'correct') AND line_no IS NOT NULL "
            "AND line_no BETWEEN 1 AND 200 "
            "AND journal_id IS NOT NULL AND net_amount IS NOT NULL AND tax_amount IS NOT NULL "
            "AND total_amount IS NOT NULL AND recognition_date IS NOT NULL "
            "AND net_amount >= 0 AND tax_amount >= 0 AND total_amount > 0 "
            "AND total_amount < 100000000000000000000 AND net_amount + tax_amount = total_amount "
            "AND recognition_date BETWEEN DATE '0001-01-01' AND DATE '9999-12-31' "
            "AND (action = 'create' OR (journal_id <> previous_journal_id "
            "AND journal_id <> reversal_journal_id))) OR (action = 'cancel' AND line_no IS NULL "
            "AND journal_id IS NULL AND net_amount IS NULL AND tax_amount IS NULL "
            "AND total_amount IS NULL AND recognition_date IS NULL)",
            name="ck_credit_revision_member_replacement",
        ),
    )
    revision_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    credit_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    document_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_line_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
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
    net_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    tax_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    recognition_date: Mapped[date | None] = mapped_column(Date, nullable=True)
