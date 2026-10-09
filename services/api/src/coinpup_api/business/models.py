"""Minimal scoped identities for immutable journal dimensions (ADR 0022)."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.models import Base


def identity_constraints(table):
    return (
        UniqueConstraint("id", "ledger_id", name=f"uq_{table}_id_ledger"),
        ForeignKeyConstraint(
            ["ledger_id"], ["ledgers.id"], name=f"fk_{table}_ledger", ondelete="RESTRICT"
        ),
        CheckConstraint("version > 0", name=f"ck_{table}_version"),
    )


class ReferenceIdentity:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ledger_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BusinessParty(ReferenceIdentity, Base):
    __tablename__ = "business_parties"
    __table_args__ = (
        *identity_constraints(__tablename__),
        CheckConstraint(
            "(name IS NULL AND role IS NULL AND legal_name IS NULL AND email IS NULL "
            "AND phone IS NULL AND address IS NULL AND tax_identifier IS NULL AND notes IS NULL) "
            "OR (name IS NOT NULL AND role IS NOT NULL AND btrim(name) <> '' "
            "AND role IN ('customer', 'supplier', 'both'))",
            name="ck_business_parties_profile",
        ),
    )

    # Legacy reference identities remain explicitly unconfigured until completed.
    name: Mapped[str | None] = mapped_column(String(160))
    role: Mapped[str | None] = mapped_column(String(8))
    legal_name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(64))
    address: Mapped[str | None] = mapped_column(String(1000))
    tax_identifier: Mapped[str | None] = mapped_column(String(128))
    notes: Mapped[str | None] = mapped_column(String(2000))


class BusinessProject(ReferenceIdentity, Base):
    __tablename__ = "business_projects"
    __table_args__ = (
        *identity_constraints(__tablename__),
        CheckConstraint("btrim(name) <> ''", name="ck_business_projects_name"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    notes: Mapped[str | None] = mapped_column(String(2000))


class BusinessDocument(ReferenceIdentity, Base):
    __tablename__ = "business_documents"
    __table_args__ = (
        *identity_constraints(__tablename__),
        ForeignKeyConstraint(
            ["party_id", "ledger_id"],
            ["business_parties.id", "business_parties.ledger_id"],
            name="fk_business_documents_party_ledger",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["asset_id"],
            ["assets.asset_id"],
            name="fk_business_documents_asset",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(document_kind IS NULL AND state IS NULL AND party_id IS NULL AND asset_id IS NULL "
            "AND issue_date IS NULL AND due_date IS NULL AND notes IS NULL "
            "AND issuer_snapshot IS NULL AND party_snapshot IS NULL) OR "
            "(document_kind IS NOT NULL AND document_kind IN ('invoice', 'bill') "
            "AND state IS NOT NULL AND state = 'draft' AND party_id IS NOT NULL "
            "AND asset_id IS NOT NULL AND issue_date IS NOT NULL "
            "AND issuer_snapshot IS NOT NULL AND jsonb_typeof(issuer_snapshot) = 'object' "
            "AND party_snapshot IS NOT NULL AND jsonb_typeof(party_snapshot) = 'object')",
            name="ck_business_documents_profile",
        ),
    )

    document_kind: Mapped[str | None] = mapped_column(String(7))
    state: Mapped[str | None] = mapped_column(String(5))
    party_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    asset_id: Mapped[str | None] = mapped_column(String(200))
    issue_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(String(2000))
    issuer_snapshot: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    party_snapshot: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))


class BusinessDocumentLine(ReferenceIdentity, Base):
    __tablename__ = "business_document_lines"
    __table_args__ = (
        *identity_constraints(__tablename__),
        UniqueConstraint(
            "id", "document_id", "ledger_id", name="uq_business_document_lines_document_ledger"
        ),
        ForeignKeyConstraint(
            ["document_id", "ledger_id"],
            ["business_documents.id", "business_documents.ledger_id"],
            name="fk_business_document_lines_document_ledger",
            ondelete="RESTRICT",
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
