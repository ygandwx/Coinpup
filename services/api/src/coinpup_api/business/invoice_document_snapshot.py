"""Freeze exact confirmed Invoice facts for a future local renderer, without issuing it."""

import hashlib
import json
from dataclasses import asdict, dataclass, replace

from pydantic import ValidationError

from coinpup_api.business.document_current import DocumentCurrent
from coinpup_api.business.invoice_issuance_schemas import InvoiceIssuanceCreate
from coinpup_api.business.pricing import MAX_DOCUMENT_LINES, price_document_rows
from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount

MAX_DOCUMENT_SNAPSHOT_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FrozenInvoiceDocument:
    content: bytes

    @property
    def sha256(self):
        return hashlib.sha256(self.content).hexdigest()

    def decode(self):
        # Each read is independent. Mutating a preview cannot change stored evidence.
        return json.loads(self.content)


def _invalid():
    return MoneyError("invoice_document_integrity", "The reviewed Invoice facts are inconsistent.")


def _json_values(value):
    """Do not admit floating point, non-string object keys, or arbitrary snapshot objects."""
    pending = [value]
    while pending:
        item = pending.pop()
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise _invalid()
            pending.extend(item.values())
        elif type(item) is list:
            pending.extend(item)
        elif item is not None and type(item) not in (str, int, bool):
            raise _invalid()


def freeze_invoice_document(current, asset, *, ledger_id, command):
    """Freeze one reviewed active version, preserving every source string and zero line.

    Callers must authorize the company ledger and read the current facts in the
    reservation transaction. This pure function neither proves freshness by itself
    nor reserves a number. The final render snapshot must additionally freeze its
    company configuration, Logo identity, assigned number and template version.
    """
    if (
        not isinstance(current, DocumentCurrent)
        or not isinstance(command, InvoiceIssuanceCreate)
        or not isinstance(asset, AssetDefinition)
    ):
        raise _invalid()
    try:
        source = current.model_dump()
        checked = DocumentCurrent.model_validate(source)
        reviewed = InvoiceIssuanceCreate.model_validate(command.model_dump())
        unit = AssetDefinition(**asdict(asset))
        document = checked.document
        if (
            checked.model_dump() != source
            or checked.ledger_id != ledger_id
            or checked.document_id != reviewed.document_id
            or checked.version != reviewed.expected_document_version
            or checked.document_kind != "invoice"
            or checked.status != "active"
            or document is None
            or document.archived
            or document.version < 2
            or document.asset_id != unit.asset_id
            or document.line_count != len(document.lines)
            or not 1 <= document.line_count <= MAX_DOCUMENT_LINES
        ):
            raise _invalid()
        rows = document.lines
        positions = [row.line_no for row in rows]
        if positions != sorted(set(positions)) or len({row.id for row in rows}) != len(rows):
            raise _invalid()
        for row in rows:
            if (
                row.archived
                or row.ledger_id != ledger_id
                or row.document_id != document.id
                or row.asset_id != unit.asset_id
                or row.category_kind != "income"
                or row.line_no < 1
            ):
                raise _invalid()
        # Validate raw JSON metadata before Pydantic's JSON serializer can turn
        # non-finite floats into null or coerce object keys/bytes into strings.
        for metadata in (
            document.issuer_snapshot,
            document.party_snapshot,
            *(row.category_snapshot for row in rows),
            *(row.project_snapshot for row in rows),
        ):
            _json_values(metadata)
        if len({row.journal_id for row in checked.postings}) != len(checked.postings):
            raise _invalid()
        # Availability may change after confirmation. It is not part of the saved unit.
        historical_unit = replace(unit, enabled=True)
        prices = price_document_rows(rows, historical_unit)
        if prices.total.minor_units <= 0:
            raise _invalid()
        for fact, price in ((document, prices), *zip(rows, prices.lines, strict=True)):
            if any(
                Amount.parse(getattr(fact, name + "_amount"), historical_unit)
                != getattr(price, name)
                for name in ("net", "tax", "total")
            ):
                raise _invalid()
        payload = {
            "schema_version": 1,
            "asset": {key: value for key, value in asdict(unit).items() if key != "enabled"},
            "current": checked.model_dump(mode="json"),
        }
        _json_values(payload)
        content = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(content) > MAX_DOCUMENT_SNAPSHOT_BYTES:
            raise _invalid()
        return FrozenInvoiceDocument(content)
    except (ValidationError, TypeError, ValueError, UnicodeError):
        raise _invalid() from None
