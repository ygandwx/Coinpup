"""Immutable version snapshots of a single refund against its original payment."""

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.business.settlement_models import SettlementIdentity, _identity, _receipt
from coinpup_api.models import Base


def _journal_reference(field):
    return ForeignKeyConstraint(
        [field + "_journal_id", "operation_id", "ledger_id"],
        ["journals.id", "journals.operation_id", "journals.ledger_id"],
        name="fk_refund_revision_" + field + "_journal",
        deferrable=True,
        initially="DEFERRED",
    )


class BusinessRefundRevision(SettlementIdentity, Base):
    __tablename__ = "business_refund_revisions"
    __table_args__ = (
        *_identity("refund_revision"),
        *_receipt("refund_revision"),
        UniqueConstraint(
            "id", "refund_id", "payment_id", "ledger_id", name="uq_refund_revision_scope"
        ),
        UniqueConstraint("refund_id", "operation_version", name="uq_refund_revision_version"),
        UniqueConstraint("reversal_journal_id", name="uq_refund_revision_reversal"),
        UniqueConstraint("replacement_journal_id", name="uq_refund_revision_replacement"),
        ForeignKeyConstraint(
            ["refund_id", "payment_id", "operation_id", "ledger_id"],
            [
                "business_refunds.id",
                "business_refunds.payment_id",
                "business_refunds.operation_id",
                "business_refunds.ledger_id",
            ],
            name="fk_refund_revision_original",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["previous_revision_id", "refund_id", "payment_id", "ledger_id"],
            [
                "business_refund_revisions.id",
                "business_refund_revisions.refund_id",
                "business_refund_revisions.payment_id",
                "business_refund_revisions.ledger_id",
            ],
            name="fk_refund_revision_predecessor",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["account_id", "asset_id", "ledger_id"],
            ["account_assets.account_id", "account_assets.asset_id", "account_assets.ledger_id"],
            name="fk_refund_revision_account_asset",
            ondelete="RESTRICT",
        ),
        _journal_reference("previous"),
        _journal_reference("reversal"),
        _journal_reference("replacement"),
        CheckConstraint(
            "(operation_version = 2 AND previous_revision_id IS NULL) OR "
            "(operation_version > 2 AND previous_revision_id IS NOT NULL "
            "AND previous_revision_id <> id)",
            name="ck_refund_revision_predecessor",
        ),
        CheckConstraint(
            "previous_journal_id <> reversal_journal_id AND "
            "(replacement_journal_id IS NULL OR (replacement_journal_id <> previous_journal_id "
            "AND replacement_journal_id <> reversal_journal_id))",
            name="ck_refund_revision_journals",
        ),
        CheckConstraint(
            "revision_reason ~ '^[^[:space:]]' AND revision_reason ~ '[^[:space:]]$'",
            name="ck_refund_revision_reason",
        ),
        CheckConstraint(
            "(action = 'correct' AND num_nonnulls(replacement_journal_id, account_id, asset_id, "
            "amount, payment_amount, transaction_date, reason, fees) = 8) OR "
            "(action = 'cancel' AND num_nonnulls(replacement_journal_id, account_id, asset_id, "
            "amount, payment_amount, transaction_date, reason, fees) = 0)",
            name="ck_refund_revision_replacement",
        ),
        CheckConstraint(
            "amount > 0 AND amount < 100000000000000000000 AND "
            "payment_amount > 0 AND payment_amount < 100000000000000000000",
            name="ck_refund_revision_amounts",
        ),
        CheckConstraint(
            "transaction_date BETWEEN DATE '0001-01-01' AND DATE '9999-12-31'",
            name="ck_refund_revision_date",
        ),
        CheckConstraint(
            "reason ~ '^[^[:space:]]' AND reason ~ '[^[:space:]]$'",
            name="ck_refund_revision_fact_reason",
        ),
        CheckConstraint(
            "CASE WHEN fees IS NULL THEN true WHEN jsonb_typeof(fees) = 'array' "
            "THEN jsonb_array_length(fees) <= 20 ELSE false END",
            name="ck_refund_revision_fees",
        ),
    )
    refund_id: Mapped[UUID] = mapped_column(Uuid)
    payment_id: Mapped[UUID] = mapped_column(Uuid)
    operation_id: Mapped[UUID] = mapped_column(Uuid)
    operation_version: Mapped[int] = mapped_column(Integer)
    previous_revision_id: Mapped[UUID | None] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(7))
    revision_reason: Mapped[str] = mapped_column(String(1000))
    previous_journal_id: Mapped[UUID] = mapped_column(Uuid)
    reversal_journal_id: Mapped[UUID] = mapped_column(Uuid)
    replacement_journal_id: Mapped[UUID | None] = mapped_column(Uuid)
    account_id: Mapped[UUID | None] = mapped_column(Uuid)
    asset_id: Mapped[str | None] = mapped_column(String(200))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    payment_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    transaction_date: Mapped[date | None] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(String(2000))
    fees: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))
    created_by: Mapped[UUID] = mapped_column(Uuid)
    idempotency_key: Mapped[str] = mapped_column(String(128))
