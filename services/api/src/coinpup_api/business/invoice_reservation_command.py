"""Reserve one immutable intent or read its original state; no public write route."""

import hmac

from sqlalchemy import select

from coinpup_api.business.invoice_issuance_hash import invoice_issuance_hash_v2
from coinpup_api.business.invoice_issuance_reads import _read_result
from coinpup_api.business.invoice_reservation_inputs import FreshInvoiceLogo, new_reservation_input
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.ledger.common import PostingCore
from coinpup_api.ledger.idempotency import _IDEMPOTENCY_KEY
from coinpup_api.ledger.models import CommandReceipt
from coinpup_api.ledger.service import LedgerError


def _conflict():
    return LedgerError(
        "idempotency_conflict", 409, "This issuance intent or key is already in use."
    )


def _reserve_or_read(session, *, owner, ledger, entity, command, key, raw_body, logo_check=None):
    """The caller has locked the owned company ledger and entity after the global write lock."""
    if entity.kind != "company":
        raise LedgerError("invoice_requires_company", 422, "Invoice issuance requires a company.")
    if type(key) is not str or not _IDEMPOTENCY_KEY.fullmatch(key):
        raise LedgerError("invalid_idempotency_key", 422, "Use a printable ASCII command key.")
    digest = invoice_issuance_hash_v2(ledger, command, raw_body=raw_body)
    existing = session.execute(
        select(InvoiceReservation.id, InvoiceReservation.request_hash).where(
            InvoiceReservation.ledger_id == ledger,
            InvoiceReservation.created_by == owner,
            InvoiceReservation.idempotency_key == key,
        )
    ).one_or_none()
    if existing is not None:
        if existing.id != command.id or not hmac.compare_digest(existing.request_hash, digest):
            raise _conflict()
        # Verify the stored original as well as the submitted digest before returning any state.
        return _read_result(session, owner, ledger, command.id)
    if (
        session.scalar(
            select(CommandReceipt.key).where(
                CommandReceipt.ledger_id == ledger, CommandReceipt.key == key
            )
        )
        is not None
    ):
        raise _conflict()
    # Do not disclose the ledger/owner or contents of a colliding global intent.
    if (
        session.scalar(select(InvoiceReservation.id).where(InvoiceReservation.id == command.id))
        is not None
    ):
        raise _conflict()
    if entity.archived:
        raise LedgerError(
            "entity_archived", 409, "Restore the company before starting an issuance."
        )
    row = new_reservation_input(
        session,
        owner=owner,
        ledger=ledger,
        command=command,
        key=key,
        raw_body=raw_body,
        logo_check=logo_check,
    )
    if isinstance(row, FreshInvoiceLogo):
        return row
    session.add(row)
    session.flush()
    return _read_result(session, owner, ledger, row.id)


class InvoiceReservationCommand(PostingCore):
    def reserve(self, owner, ledger, command, key, *, raw_body=None, logo_check=None):
        with self._transaction(owner, write=True, read_committed=True) as session:
            _, entity = self._locked_ledger(session, owner, ledger)
            return _reserve_or_read(
                session,
                owner=owner,
                ledger=ledger,
                entity=entity,
                command=command,
                key=key,
                raw_body=raw_body,
                logo_check=logo_check,
            )
