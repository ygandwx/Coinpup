"""One immutable completion binds the two private PDFs to its original reservation."""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Integer, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.business.settlement_models import SettlementIdentity, _identity
from coinpup_api.models import Base


class InvoiceCompletion(SettlementIdentity, Base):
    __tablename__ = "business_invoice_completions"
    __table_args__ = (
        *_identity("invoice_completion"),
        ForeignKeyConstraint(
            ["id", "ledger_id"],
            ["business_invoice_reservations.id", "business_invoice_reservations.ledger_id"],
            name="fk_invoice_completion_reservation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ledger_id", "created_by"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_invoice_completion_owner",
            ondelete="RESTRICT",
        ),
        *(
            ForeignKeyConstraint(
                [f"{language}_file_id", "ledger_id"],
                ["stored_files.id", "stored_files.ledger_id"],
                name=f"fk_invoice_completion_{language}_file",
                ondelete="RESTRICT",
            )
            for language in ("zh", "en")
        ),
        *(
            UniqueConstraint(f"{language}_file_id", name=f"uq_invoice_completion_{language}_file")
            for language in ("zh", "en")
        ),
        CheckConstraint("zh_file_id <> en_file_id", name="ck_invoice_completion_distinct_files"),
        CheckConstraint("response_status = 201", name="ck_invoice_completion_status"),
        CheckConstraint(
            "jsonb_typeof(response) = 'object' AND octet_length(response::text) <= 65536",
            name="ck_invoice_completion_response_size",
        ),
    )

    zh_file_id: Mapped[UUID] = mapped_column(Uuid)
    en_file_id: Mapped[UUID] = mapped_column(Uuid)
    created_by: Mapped[UUID] = mapped_column(Uuid)
    response: Mapped[dict] = mapped_column(JSONB)
    response_status: Mapped[int] = mapped_column(Integer, server_default="201")
