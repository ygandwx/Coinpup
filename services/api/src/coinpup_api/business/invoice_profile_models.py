"""Company-owned current Invoice configuration; issuance freezes its reviewed version."""

import uuid

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.business.models import ReferenceIdentity, identity_constraints
from coinpup_api.models import Base


class InvoiceProfile(ReferenceIdentity, Base):
    __tablename__ = "business_invoice_profiles"
    __table_args__ = (
        *identity_constraints(__tablename__),
        ForeignKeyConstraint(
            ["logo_file_id", "ledger_id"],
            ["stored_files.id", "stored_files.ledger_id"],
            name="fk_invoice_profile_logo_ledger",
            ondelete="RESTRICT",
        ),
        CheckConstraint("btrim(name) <> ''", name="ck_invoice_profile_name"),
        CheckConstraint(
            "number_prefix = btrim(number_prefix) AND number_prefix !~ '[[:cntrl:]]'",
            name="ck_invoice_profile_prefix",
        ),
        CheckConstraint("minimum_digits BETWEEN 1 AND 20", name="ck_invoice_profile_digits"),
        CheckConstraint(
            "(logo_file_id IS NULL AND logo_file_version IS NULL) OR "
            "(logo_file_id IS NOT NULL AND logo_file_version IS NOT NULL "
            "AND logo_file_version > 0)",
            name="ck_invoice_profile_logo_review",
        ),
        CheckConstraint("updated_at >= created_at", name="ck_invoice_profile_timestamps"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    number_prefix: Mapped[str] = mapped_column(String(40), nullable=False)
    minimum_digits: Mapped[int] = mapped_column(Integer, nullable=False)
    issuer_extra_text: Mapped[str | None] = mapped_column(String(2000))
    payment_instructions: Mapped[str | None] = mapped_column(String(2000))
    footer: Mapped[str | None] = mapped_column(String(2000))
    logo_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    logo_file_version: Mapped[int | None] = mapped_column(Integer)
