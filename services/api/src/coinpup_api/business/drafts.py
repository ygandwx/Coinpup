"""Atomic draft creation and snapshot reads; never creates financial operations."""

from dataclasses import replace
from decimal import Decimal

from sqlalchemy import and_, func, or_, select

from coinpup_api.business.draft_schemas import DraftLineResponse, DraftResponse, DraftSummary
from coinpup_api.business.models import (
    BusinessDocument,
    BusinessDocumentLine,
    BusinessParty,
    BusinessProject,
)
from coinpup_api.business.pricing import PriceInput, price_document
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import AssetRecord, Category
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.service import (
    LedgerError,
    LedgerService,
    _not_found,
    _page,
    _touch,
    _version,
)

PRICE_FIELDS = ("quantity", "unit_price", "discount_amount", "tax_rate_percent")
AMOUNTS = ("net", "tax", "total")


def _snapshot(row, fields):
    return dict(id=str(row.id), version=row.version, **{f: getattr(row, f) for f in fields})


def _source(line):
    return PriceInput(**{field: getattr(line, field) for field in PRICE_FIELDS})


def _fields(row, schema, **overrides):
    return {
        name: getattr(row, name) for name in schema.model_fields if name not in overrides
    } | overrides


def _amounts(price):
    return {name + "_amount": getattr(price, name).to_string() for name in AMOUNTS}


def _references(session, model, ledger_id, identifiers):
    rows = {
        row.id: row
        for row in session.scalars(
            select(model).where(model.ledger_id == ledger_id, model.id.in_(identifiers))
        )
    }
    if set(rows) != identifiers:
        raise _not_found()
    if any(row.archived for row in rows.values()):
        raise LedgerError("reference_archived", 409, "Restore or replace the archived reference.")
    return rows


def _response(document, lines, asset):
    # Availability governs new input, not reading saved history.
    asset = replace(asset, enabled=True)
    price = price_document([_source(line) for line in lines], asset)
    results = []
    for line, calculated in zip(lines, price.lines, strict=True):
        if any(
            Amount.from_decimal(getattr(line, name + "_amount"), asset) != getattr(calculated, name)
            for name in AMOUNTS
        ):
            raise LedgerError("draft_integrity", 409, "Stored draft pricing is inconsistent.")
        results.append(
            DraftLineResponse(**_fields(line, DraftLineResponse, **_amounts(calculated)))
        )
    return DraftResponse(
        **_fields(document, DraftResponse, lines=results, line_count=len(lines), **_amounts(price))
    )


class DraftService(LedgerService):
    def _prepare(self, session, ledger_id, entity, payload):
        asset = asset_definition(self._assets(session, {payload.asset_id})[0])
        try:
            price = price_document([_source(line) for line in payload.lines], asset)
        except MoneyError as error:
            raise LedgerError(error.code, 422, "Draft pricing input is invalid.") from None
        party = _references(session, BusinessParty, ledger_id, {payload.party_id})[payload.party_id]
        role = "customer" if payload.document_kind == "invoice" else "supplier"
        if not party.name or party.role not in (role, "both"):
            raise LedgerError("party_role", 422, "The party profile does not support this draft.")
        categories = _references(
            session, Category, ledger_id, {line.category_id for line in payload.lines}
        )
        projects = _references(
            session,
            BusinessProject,
            ledger_id,
            {line.project_id for line in payload.lines if line.project_id is not None},
        )
        kind = "income" if payload.document_kind == "invoice" else "expense"
        if any(row.kind != kind for row in categories.values()):
            raise LedgerError(
                "category_kind", 422, "The category does not match the document kind."
            )
        document = dict(
            ledger_id=ledger_id,
            state="draft",
            **payload.model_dump(exclude={"lines", "id", "expected_version", "refresh_snapshots"}),
            issuer_snapshot=_snapshot(
                entity,
                (
                    "name",
                    "legal_name",
                    "country_code",
                    "region_code",
                    "company_type",
                ),
            ),
            party_snapshot=_snapshot(
                party,
                (
                    "name",
                    "role",
                    "legal_name",
                    "email",
                    "phone",
                    "address",
                    "tax_identifier",
                ),
            ),
        )
        lines = []
        for position, (source, calculated) in enumerate(
            zip(payload.lines, price.lines, strict=True), 1
        ):
            line = dict(
                ledger_id=ledger_id,
                line_no=position,
                asset_id=asset.asset_id,
                category_kind=kind,
                **source.model_dump(),
                category_snapshot=_snapshot(
                    categories[source.category_id], ("name", "name_en", "kind")
                ),
                project_snapshot=(
                    _snapshot(projects[source.project_id], ("name",)) if source.project_id else None
                ),
                **{name + "_amount": getattr(calculated, name).to_decimal() for name in AMOUNTS},
            )
            lines.append(line)
        return document, lines, asset

    def create_draft(self, owner_id, ledger_id, payload):
        with self._transaction(owner_id, write=True) as session:
            _, entity = self._ledger(session, owner_id, ledger_id, write=True)
            header, prepared, asset = self._prepare(session, ledger_id, entity, payload)
            document = BusinessDocument(id=payload.id, **header)
            session.add(document)
            session.flush()
            lines = [BusinessDocumentLine(document_id=document.id, **values) for values in prepared]
            session.add_all(lines)
            session.flush()
            return _response(document, lines, asset)

    def update_draft(self, owner_id, ledger_id, identifier, payload):
        with self._transaction(owner_id, write=True) as session:
            _, entity = self._ledger(session, owner_id, ledger_id, write=True)
            document = self._draft(session, ledger_id, identifier)
            _version(document, payload.expected_version)
            if document.archived:
                raise LedgerError("draft_archived", 409, "Restore the draft before editing.")
            # Lock both old and new identities in order; only new input must be enabled.
            session.scalars(
                select(AssetRecord)
                .where(AssetRecord.asset_id.in_({document.asset_id, payload.asset_id}))
                .order_by(AssetRecord.asset_id)
                .with_for_update(read=True)
            ).all()
            # No ORM mutation until all input and reference checks have succeeded.
            header, prepared, asset = self._prepare(session, ledger_id, entity, payload)
            identifiers = {line["id"] for line in prepared}
            model = BusinessDocumentLine
            existing = {
                row.id: row
                for row in session.scalars(
                    select(model).where(
                        or_(
                            and_(
                                model.document_id == identifier,
                                model.ledger_id == ledger_id,
                                model.archived.is_(False),
                            ),
                            model.id.in_(identifiers),
                        )
                    )
                )
            }
            if any(
                row.document_id != identifier or row.ledger_id != ledger_id
                for row in existing.values()
            ):
                raise _not_found()
            position = (
                session.scalar(
                    select(func.max(model.line_no)).where(
                        model.document_id == identifier,
                        model.ledger_id == ledger_id,
                    )
                )
                or 0
            )
            if position + len(identifiers - existing.keys()) > 2147483647:
                raise LedgerError("draft_line_limit", 422, "Draft line identities are exhausted.")
            if not payload.refresh_snapshots:
                header["issuer_snapshot"] = document.issuer_snapshot
                if document.party_id == payload.party_id:
                    header["party_snapshot"] = document.party_snapshot
            for field, value in header.items():
                setattr(document, field, value)
            _touch(document)
            for values in prepared:
                row = existing.get(values["id"])
                if row is None:
                    position += 1
                    values["line_no"] = position
                    session.add(model(document_id=identifier, **values))
                    continue
                values.pop("line_no")
                if not payload.refresh_snapshots:
                    for key in ("category", "project"):
                        if getattr(row, key + "_id") == values[key + "_id"]:
                            values[key + "_snapshot"] = getattr(row, key + "_snapshot")
                values["archived"] = False
                changed = {
                    key: value for key, value in values.items() if getattr(row, key) != value
                }
                if changed:
                    for key, value in changed.items():
                        setattr(row, key, value)
                    _touch(row)
            for row in existing.values():
                if row.id not in identifiers and not row.archived:
                    row.archived = True
                    _touch(row)
            session.flush()
            session.refresh(document)
            return _response(document, self._lines(session, ledger_id, identifier), asset)

    def set_draft_archived(self, owner_id, ledger_id, identifier, payload):
        with self._transaction(owner_id, write=True) as session:
            self._ledger(session, owner_id, ledger_id, write=True)
            document = self._draft(session, ledger_id, identifier)
            _version(document, payload.expected_version)
            # Archive/restore keeps historical input; disabled assets are still readable.
            record = session.scalar(
                select(AssetRecord)
                .where(
                    AssetRecord.asset_id == document.asset_id,
                )
                .with_for_update(read=True)
            )
            document.archived = payload.archived
            _touch(document)
            session.flush()
            session.refresh(document)
            return _response(
                document, self._lines(session, ledger_id, identifier), asset_definition(record)
            )

    @staticmethod
    def _draft(session, ledger_id, identifier):
        document = session.scalar(
            select(BusinessDocument).where(
                BusinessDocument.id == identifier,
                BusinessDocument.ledger_id == ledger_id,
                BusinessDocument.document_kind.is_not(None),
                BusinessDocument.state == "draft",
            )
        )
        if document is None:
            raise _not_found()
        return document

    @staticmethod
    def _lines(session, ledger_id, identifier):
        return session.scalars(
            select(BusinessDocumentLine)
            .where(
                BusinessDocumentLine.document_id == identifier,
                BusinessDocumentLine.ledger_id == ledger_id,
                BusinessDocumentLine.archived.is_(False),
            )
            .order_by(BusinessDocumentLine.line_no)
        ).all()

    def get_draft(self, owner_id, ledger_id, identifier):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            document = self._draft(session, ledger_id, identifier)
            lines = self._lines(session, ledger_id, identifier)
            return _response(
                document, lines, asset_definition(session.get(AssetRecord, document.asset_id))
            )

    def list_drafts(self, owner_id, ledger_id, *, include_archived=True, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            statement = select(BusinessDocument).where(
                BusinessDocument.ledger_id == ledger_id,
                BusinessDocument.document_kind.is_not(None),
            )
            if not include_archived:
                statement = statement.where(BusinessDocument.archived.is_(False))
            documents = session.scalars(
                statement.order_by(
                    BusinessDocument.created_at,
                    BusinessDocument.id,
                )
                .limit(limit)
                .offset(offset)
            ).all()
            if not documents:
                return []
            line = BusinessDocumentLine
            totals = {
                row[0]: row[1:]
                for row in session.execute(
                    select(
                        line.document_id,
                        func.count(line.id),
                        *(func.sum(getattr(line, name + "_amount")) for name in AMOUNTS),
                    )
                    .where(
                        line.ledger_id == ledger_id,
                        line.document_id.in_([d.id for d in documents]),
                        line.archived.is_(False),
                    )
                    .group_by(line.document_id)
                )
            }
            assets = {
                row.asset_id: asset_definition(row)
                for row in session.scalars(
                    select(AssetRecord).where(
                        AssetRecord.asset_id.in_({d.asset_id for d in documents})
                    )
                )
            }
            results = []
            for document in documents:
                count, *values = totals.get(document.id, (0, Decimal(0), Decimal(0), Decimal(0)))
                amounts = {
                    name + "_amount": Amount.from_decimal(
                        value, assets[document.asset_id]
                    ).to_string()
                    for name, value in zip(AMOUNTS, values, strict=True)
                }
                results.append(
                    DraftSummary(**_fields(document, DraftSummary, line_count=count, **amounts))
                )
            return results
