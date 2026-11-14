"""Read the fixed payment and exact last normal posting for a single authorized refund."""

from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from coinpup_api.business.refund_models import BusinessRefund
from coinpup_api.business.refund_revision_models import BusinessRefundRevision
from coinpup_api.business.refund_revision_receipts import integrity
from coinpup_api.business.refund_revision_schemas import RefundRevisionFact
from coinpup_api.business.settlement_models import BusinessPayment
from coinpup_api.ledger.models import FinancialOperation, Journal, JournalLine
from coinpup_api.ledger.service import _not_found


@dataclass(frozen=True, slots=True)
class CurrentRefund:
    refund: BusinessRefund
    payment: BusinessPayment
    payment_operation: FinancialOperation
    operation: FinancialOperation
    revision: BusinessRefundRevision | None
    journal: Journal
    lines: tuple[JournalLine, ...]
    fact: RefundRevisionFact


def read_current_refund(session, ledger_id, refund_id):
    """Use a caller-authorized, consistent read or ordered write transaction."""
    payer = aliased(FinancialOperation)
    record = session.execute(
        select(BusinessRefund, BusinessPayment, payer, FinancialOperation, Journal)
        .select_from(BusinessRefund)
        .outerjoin(
            BusinessPayment,
            (BusinessPayment.id == BusinessRefund.payment_id)
            & (BusinessPayment.ledger_id == BusinessRefund.ledger_id),
        )
        .outerjoin(
            payer,
            (payer.id == BusinessPayment.operation_id)
            & (payer.ledger_id == BusinessPayment.ledger_id),
        )
        .outerjoin(
            FinancialOperation,
            (FinancialOperation.id == BusinessRefund.operation_id)
            & (FinancialOperation.ledger_id == BusinessRefund.ledger_id),
        )
        .outerjoin(
            Journal,
            (Journal.id == FinancialOperation.current_journal_id)
            & (Journal.operation_id == FinancialOperation.id)
            & (Journal.ledger_id == FinancialOperation.ledger_id),
        )
        .where(BusinessRefund.id == refund_id, BusinessRefund.ledger_id == ledger_id)
    ).one_or_none()
    if record is None:
        raise _not_found()
    refund, payment, payment_operation, operation, journal = record
    if (
        payment is None
        or payment_operation is None
        or payment_operation.kind != "payment"
        or operation is None
        or operation.kind != "refund"
        or operation.created_by != refund.created_by
        or journal is None
        or not journal.sealed
        or journal.journal_kind != "posting"
        or journal.reverses_journal_id is not None
    ):
        raise integrity()
    revision = session.scalar(
        select(BusinessRefundRevision)
        .where(
            BusinessRefundRevision.refund_id == refund_id,
            BusinessRefundRevision.ledger_id == ledger_id,
        )
        .order_by(BusinessRefundRevision.operation_version.desc())
        .limit(1)
    )
    if revision is None:
        version, journal_id, fact_revision, cancelled = 1, refund.journal_id, None, False
    else:
        if (
            revision.operation_id != operation.id
            or revision.payment_id != payment.id
            or revision.action not in {"correct", "cancel"}
        ):
            raise integrity()
        version = revision.operation_version
        cancelled = revision.action == "cancel"
        journal_id = revision.previous_journal_id if cancelled else revision.replacement_journal_id
        fact_revision = revision.previous_revision_id if cancelled else revision.id
    if (
        operation.version != version
        or operation.status != ("cancelled" if cancelled else "active")
        or journal.id != journal_id
        or journal.operation_version != (version - 1 if cancelled else version)
    ):
        raise integrity()
    try:
        row = (
            session.execute(
                text("SELECT * FROM public.coinpup_refund_posting_fact(:ledger,:journal)"),
                {"ledger": ledger_id, "journal": journal_id},
            )
            .mappings()
            .one_or_none()
        )
    except IntegrityError as error:
        if (
            getattr(error.orig, "sqlstate", None) == "23514"
            and getattr(getattr(error.orig, "diag", None), "constraint_name", None)
            == "ck_refund_posting_fact"
        ):
            raise integrity() from None
        raise
    if (
        row is None
        or row["refund_id"] != refund_id
        or row["payment_id"] != payment.id
        or row["operation_id"] != operation.id
        or row["journal_id"] != journal_id
        or row["ledger_id"] != ledger_id
        or row["operation_version"] != journal.operation_version
        or row["revision_id"] != fact_revision
        or row["source_version"] != refund.source_version
        or row["payment_asset_id"] != payment.asset_id
        or row["reason"] != journal.description
        or row["revision_reason"] != journal.revision_reason
        or row["transaction_date"] != journal.transaction_date
        or row["recognition_date"] != journal.recognition_date
    ):
        raise integrity()
    try:
        fact = RefundRevisionFact.model_validate(
            {field: row[field] for field in RefundRevisionFact.model_fields}
            | {
                "amount": format(row["amount"], "f"),
                "payment_amount": format(row["payment_amount"], "f"),
            }
        )
    except ValidationError:
        raise integrity() from None
    lines = tuple(
        session.scalars(
            select(JournalLine)
            .where(JournalLine.journal_id == journal_id, JournalLine.ledger_id == ledger_id)
            .order_by(JournalLine.line_no)
            .limit(45)
        )
    )
    principal_legs = 2 if fact.asset_id == fact.payment_asset_id else 4
    if len(lines) != principal_legs + 2 * len(fact.fees):
        raise integrity()
    return CurrentRefund(
        refund, payment, payment_operation, operation, revision, journal, lines, fact
    )
