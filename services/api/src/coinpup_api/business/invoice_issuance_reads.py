"""Read the state of one owned issuance without allocating a number or rendering files."""

from sqlalchemy import and_, select
from sqlalchemy.orm import aliased

from coinpup_api.business.invoice_completion_facts import verify_invoice_completion
from coinpup_api.business.invoice_completion_models import InvoiceCompletion
from coinpup_api.business.invoice_issuance_results import InvoiceIssuancePending
from coinpup_api.business.invoice_reservation_facts import verify_invoice_reservation
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.files.models import StoredFile
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.service import LedgerError, LedgerService, _not_found


def _read_result(session, owner, ledger, identifier):
    """Caller has authorized the company ledger in this transaction."""
    zh, en = aliased(StoredFile), aliased(StoredFile)
    reservation, completed = InvoiceReservation, InvoiceCompletion
    # The reservation ID is globally unique. Join the completion by that identity
    # so a corrupt completion fails verification instead of appearing absent.
    statement = (
        select(reservation, completed, zh, en)
        .select_from(reservation)
        .outerjoin(completed, completed.id == reservation.id)
        .outerjoin(zh, and_(zh.id == completed.zh_file_id, zh.ledger_id == reservation.ledger_id))
        .outerjoin(en, and_(en.id == completed.en_file_id, en.ledger_id == reservation.ledger_id))
        .where(
            reservation.id == identifier,
            reservation.ledger_id == ledger,
            reservation.created_by == owner,
        )
    )
    rows = session.execute(statement).one_or_none()
    if rows is None:
        raise _not_found()
    row, completion, zh_file, en_file = rows
    try:
        if completion is None:
            saved = verify_invoice_reservation(row, owner_id=owner, ledger_id=ledger)
            return InvoiceIssuancePending(
                **saved.identity.model_dump(), state="pending", artifacts=()
            )
        return verify_invoice_completion(
            row, completion, zh_file=zh_file, en_file=en_file, owner_id=owner, ledger_id=ledger
        )
    except MoneyError as error:
        raise LedgerError(error.code, 503, "Stored Invoice evidence is inconsistent.") from None


class InvoiceIssuanceReads(LedgerService):
    def get(self, owner, ledger, identifier):
        with self._transaction(owner, read_only=True) as session:
            _, entity = self._ledger(session, owner, ledger)
            if entity.kind != "company":
                raise LedgerError(
                    "invoice_requires_company", 422, "Invoice issuance requires a company."
                )
            return _read_result(session, owner, ledger, identifier)
