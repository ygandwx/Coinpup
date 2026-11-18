"""Owned original mappings and fresh remaining quantities after the settlement lock."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Bundle

from coinpup_api.business.allocation_document_facts import allocation_document_facts
from coinpup_api.business.allocation_return_plan import AllocationReturnSource
from coinpup_api.business.allocation_return_projections import return_source_state
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
    source, group, operation = BusinessAllocationLine, BusinessAllocation, FinancialOperation
    requested = {item.source_allocation_line_id: item for item in payload.lines}
    facts = allocation_document_facts(ledger_id, line_ids=requested)
    f = facts.c
    document = Bundle(
        "document", f.document_id.label("id"), f.document_kind, f.asset_id, f.party_id, f.issue_date
    )
    line = Bundle(
        "line",
        f.source_line_id.label("id"),
        f.project_id,
        f.net_amount,
        f.tax_amount,
        f.total_amount,
    )
    rows = session.execute(
        select(source, group, document, line, operation)
        .select_from(source)
        .join(group, (group.id == source.allocation_id) & (group.ledger_id == source.ledger_id))
        .join(facts, (f.allocation_line_id == source.id) & (f.ledger_id == source.ledger_id))
        .join(
            operation,
            (operation.id == source.operation_id) & (operation.ledger_id == source.ledger_id),
        )
        .where(
            source.ledger_id == ledger_id,
            source.id.in_(requested),
            source.payment_id == payload.payment_id,
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
    totals = return_source_state(session, ledger_id, [mapping.id for mapping, *_ in rows])
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
        returned_p, returned_d, last_position = totals.get(mapping.id, (Decimal(0), Decimal(0), 0))
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
        next_positions[mapping.id] = last_position + 1
    return payer, sources, next_positions
