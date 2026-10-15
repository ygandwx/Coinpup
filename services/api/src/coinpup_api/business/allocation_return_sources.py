"""Owned original mappings and fresh remaining quantities after the settlement lock."""

from decimal import Decimal

from sqlalchemy import func, select

from coinpup_api.business.allocation_return_models import BusinessAllocationReturnLine
from coinpup_api.business.allocation_return_plan import AllocationReturnSource
from coinpup_api.business.models import BusinessDocument, BusinessDocumentLine
from coinpup_api.business.settlement_models import BusinessAllocation, BusinessAllocationLine
from coinpup_api.business.settlement_plan import PaymentSource
from coinpup_api.business.settlement_totals import applied_totals
from coinpup_api.ledger.models import FinancialOperation
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.service import LedgerError, _not_found


def require_current(operation, expected_version, journal_id, kind):
    if operation.version != expected_version:
        raise LedgerError("version_conflict", 409, "The original operation version changed.")
    if (
        operation.kind != kind
        or operation.status != "active"
        or operation.version != 1
        or operation.current_journal_id != journal_id
    ):
        raise LedgerError("settlement_inactive", 409, "The original settlement is not active.")


def original_sources(session, ledger_id, payload):
    source, group, doc, line, operation = (
        BusinessAllocationLine,
        BusinessAllocation,
        BusinessDocument,
        BusinessDocumentLine,
        FinancialOperation,
    )
    requested = {item.source_allocation_line_id: item for item in payload.lines}
    rows = session.execute(
        select(source, group, doc, line, operation)
        .select_from(source)
        .join(group, group.id == source.allocation_id)
        .join(doc, doc.id == source.document_id)
        .join(line, (line.id == source.document_line_id) & (line.document_id == doc.id))
        .join(operation, operation.id == source.operation_id)
        .where(
            *(table.ledger_id == ledger_id for table in (source, group, doc, line, operation)),
            source.id.in_(requested),
            source.payment_id == payload.payment_id,
            doc.state == "confirmed",
        )
    ).all()
    if {row[0].id for row in rows} != requested.keys():
        raise _not_found()
    for mapping, _group, _doc, _line, op in rows:
        require_current(
            op, requested[mapping.id].expected_version, mapping.journal_id, "allocation"
        )
    return rows


def plan_sources(session, ledger_id, payment, rows, catalog):
    line = BusinessAllocationReturnLine
    totals = {
        row[0]: row[1:]
        for row in session.execute(
            select(
                line.source_allocation_line_id,
                func.sum(line.payment_amount),
                func.sum(line.document_amount),
                func.count(line.id),
            )
            .where(
                line.ledger_id == ledger_id,
                line.source_allocation_line_id.in_([row[0].id for row in rows]),
            )
            .group_by(line.source_allocation_line_id)
        )
    }
    asset = catalog[payment.asset_id]
    used = applied_totals(session, ledger_id, {payment.id}).get(payment.id, Decimal(0))
    payer = PaymentSource(
        payment.id,
        ledger_id,
        payment.party_id,
        payment.direction,
        payment.transaction_date,
        Amount.from_decimal(payment.amount, asset),
        Amount.from_decimal(used, asset),
    )
    sources, next_positions = {}, {}
    for mapping, group, document, source, _operation in rows:
        returned_p, returned_d, count = totals.get(mapping.id, (Decimal(0), Decimal(0), 0))
        document_asset = catalog[document.asset_id]
        sources[mapping.id] = AllocationReturnSource(
            mapping.id,
            ledger_id,
            payment.id,
            document.party_id,
            mapping.operation_id,
            group.allocation_date,
            document.id,
            source.id,
            document.document_kind,
            document.issue_date,
            source.project_id,
            Amount.from_decimal(mapping.payment_amount, asset),
            Amount.from_decimal(mapping.document_amount, document_asset),
            Amount.from_decimal(returned_p, asset),
            Amount.from_decimal(returned_d, document_asset),
        )
        next_positions[mapping.id] = count + 1
    return payer, sources, next_positions
