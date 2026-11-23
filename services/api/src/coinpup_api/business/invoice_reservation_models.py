"""Immutable Invoice reservation evidence; complete reservation writes remain gated."""

from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.business.settlement_models import SettlementIdentity, _identity
from coinpup_api.models import Base


class InvoiceReservation(SettlementIdentity, Base):
    __tablename__ = "business_invoice_reservations"
    __table_args__ = (
        *_identity("invoice_reservation"),
        UniqueConstraint("ledger_id", "idempotency_key", name="uq_invoice_reservation_key"),
        UniqueConstraint("ledger_id", "sequence", name="uq_invoice_reservation_sequence"),
        UniqueConstraint("ledger_id", "number", name="uq_invoice_reservation_number"),
        UniqueConstraint(
            "ledger_id", "document_id", "document_version", name="uq_invoice_reservation_version"
        ),
        UniqueConstraint(
            "id", "document_id", "ledger_id", name="uq_invoice_reservation_document_scope"
        ),
        ForeignKeyConstraint(
            ["ledger_id", "created_by"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_invoice_reservation_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_id", "ledger_id"],
            ["business_documents.id", "business_documents.ledger_id"],
            name="fk_invoice_reservation_document",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["configuration_id", "ledger_id"],
            ["business_invoice_profiles.id", "business_invoice_profiles.ledger_id"],
            name="fk_invoice_reservation_configuration",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["previous_issuance_id", "document_id", "ledger_id"],
            [
                "business_invoice_reservations.id",
                "business_invoice_reservations.document_id",
                "business_invoice_reservations.ledger_id",
            ],
            name="fk_invoice_reservation_previous",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["logo_file_id", "ledger_id"],
            ["stored_files.id", "stored_files.ledger_id"],
            name="fk_invoice_reservation_logo",
            ondelete="RESTRICT",
        ),
        CheckConstraint("sequence > 0", name="ck_invoice_reservation_sequence"),
        CheckConstraint(
            "document_version >= 2 AND configuration_version > 0",
            name="ck_invoice_reservation_versions",
        ),
        CheckConstraint(
            "number <> '' AND number !~ '[[:cntrl:]]'", name="ck_invoice_reservation_number"
        ),
        CheckConstraint(
            "previous_issuance_id IS NULL OR previous_issuance_id <> id",
            name="ck_invoice_reservation_previous",
        ),
        CheckConstraint(
            "hash_version = 2 AND request_hash ~ '^[0-9a-f]{64}$'",
            name="ck_invoice_reservation_request_hash",
        ),
        CheckConstraint(
            "snapshot_sha256 ~ '^[0-9a-f]{64}$'", name="ck_invoice_reservation_snapshot_hash"
        ),
        CheckConstraint(
            "template_version ~ '^[a-z0-9][a-z0-9._-]{0,63}$'",
            name="ck_invoice_reservation_template",
        ),
        CheckConstraint("idempotency_key ~ '^[!-~]{1,128}$'", name="ck_invoice_reservation_key"),
        CheckConstraint(
            "octet_length(request_json) BETWEEN 2 AND 65536",
            name="ck_invoice_reservation_request_size",
        ),
        CheckConstraint(
            "octet_length(snapshot_json) BETWEEN 2 AND 4259840",
            name="ck_invoice_reservation_snapshot_size",
        ),
    )

    document_id: Mapped[UUID] = mapped_column(Uuid)
    document_version: Mapped[int] = mapped_column(Integer)
    configuration_id: Mapped[UUID] = mapped_column(Uuid)
    configuration_version: Mapped[int] = mapped_column(Integer)
    previous_issuance_id: Mapped[UUID | None] = mapped_column(Uuid)
    logo_file_id: Mapped[UUID | None] = mapped_column(Uuid)
    sequence: Mapped[int] = mapped_column(BigInteger)
    number: Mapped[str] = mapped_column(String(60))
    template_version: Mapped[str] = mapped_column(String(64))
    snapshot_json: Mapped[str] = mapped_column(Text)
    snapshot_sha256: Mapped[str] = mapped_column(String(64))
    request_json: Mapped[str] = mapped_column(Text)
    request_hash: Mapped[str] = mapped_column(String(64))
    hash_version: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_by: Mapped[UUID] = mapped_column(Uuid)
