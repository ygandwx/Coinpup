"""Current sealed members for a whole return revision in a caller-owned ledger snapshot."""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, true
from sqlalchemy.orm import aliased

from coinpup_api.business.allocation_return_models import (
    BusinessAllocationReturn as Return,
)
from coinpup_api.business.allocation_return_models import (
    BusinessAllocationReturnLine,
)
from coinpup_api.business.allocation_return_posting_facts import return_posting_facts
from coinpup_api.business.allocation_return_receipts import integrity
from coinpup_api.business.allocation_return_revision_member_models import (
    BusinessReturnRevisionMember,
)
from coinpup_api.business.allocation_return_revision_models import BusinessAllocationReturnRevision
from coinpup_api.business.settlement_models import BusinessPayment
from coinpup_api.ledger.models import FinancialOperation, Journal, JournalLine
from coinpup_api.ledger.service import _not_found


@dataclass(frozen=True, slots=True)
class CurrentReturnMember:
    fact: Mapping
    operation: FinancialOperation
    journal: Journal
    lines: tuple[JournalLine, ...]


@dataclass(frozen=True, slots=True)
class CurrentReturn:
    group: Return
    payment: BusinessPayment
    payment_operation: FinancialOperation
    revision: BusinessAllocationReturnRevision | None
    members: tuple[CurrentReturnMember, ...]

    @property
    def version(self):
        return self.revision.group_version if self.revision else 1

    @property
    def cancelled(self):
        return self.revision is not None and self.revision.action == "cancel"


def read_current_return(session, ledger_id: UUID, return_id: UUID) -> CurrentReturn:
    """Read up to 200 active postings after the caller locks the ledger for a write."""
    header = BusinessAllocationReturnRevision
    latest = (
        select(header)
        .where(
            header.ledger_id == Return.ledger_id,
            header.payment_id == Return.payment_id,
            header.return_id == Return.id,
        )
        .order_by(header.group_version.desc())
        .limit(1)
        .correlate(Return)
        .lateral()
    )
    revision = aliased(header, latest)
    with session.no_autoflush:
        row = session.execute(
            select(Return, BusinessPayment, FinancialOperation, revision)
            .select_from(Return)
            .join(
                BusinessPayment,
                (BusinessPayment.id == Return.payment_id)
                & (BusinessPayment.ledger_id == Return.ledger_id),
            )
            .join(
                FinancialOperation,
                (FinancialOperation.id == BusinessPayment.operation_id)
                & (FinancialOperation.ledger_id == Return.ledger_id),
            )
            .outerjoin(revision, true())
            .where(Return.id == return_id, Return.ledger_id == ledger_id)
        ).one_or_none()
        if row is None:
            raise _not_found()
        group, payment, payment_operation, saved = row
        if saved is not None and saved.action == "cancel":
            return CurrentReturn(group, payment, payment_operation, saved, ())
        members = _active_members(session, group, payment, saved)
        return CurrentReturn(group, payment, payment_operation, saved, members)


def _active_members(session, group, payment, revision):
    if revision is None:
        table = BusinessAllocationReturnLine
        candidates = select(table.journal_id).where(table.return_id == group.id)
    else:
        table = BusinessReturnRevisionMember
        candidates = select(table.journal_id).where(
            table.return_id == group.id,
            table.revision_id == revision.id,
            table.action.in_(("correct", "create")),
        )
    identifiers = session.scalars(
        candidates.where(table.ledger_id == group.ledger_id, table.payment_id == payment.id)
        .order_by(table.line_no)
        .limit(201)
    ).all()
    if not 1 <= len(identifiers) <= 200 or len(set(identifiers)) != len(identifiers):
        raise integrity()
    facts = return_posting_facts(group.ledger_id, return_ids=[group.id], journal_ids=identifiers)
    rows = (
        session.execute(
            select(facts, FinancialOperation, Journal)
            .select_from(facts)
            .join(
                FinancialOperation,
                (FinancialOperation.id == facts.c.operation_id)
                & (FinancialOperation.ledger_id == facts.c.ledger_id),
            )
            .join(
                Journal,
                (Journal.id == facts.c.journal_id) & (Journal.ledger_id == facts.c.ledger_id),
            )
            .order_by(facts.c.line_no)
            .limit(201)
        )
        .mappings()
        .all()
    )
    if len(rows) != len(identifiers):
        raise integrity()
    by_journal = {identifier: [] for identifier in identifiers}
    for line in session.scalars(
        select(JournalLine)
        .where(JournalLine.ledger_id == group.ledger_id, JournalLine.journal_id.in_(identifiers))
        .order_by(JournalLine.journal_id, JournalLine.line_no)
        .limit(801)
    ):
        by_journal[line.journal_id].append(line)

    members = []
    for position, (identifier, row) in enumerate(zip(identifiers, rows, strict=True), 1):
        operation, journal = row[FinancialOperation], row[Journal]
        fact = {field: row[field] for field in facts.c.keys()}
        lines = by_journal[identifier]
        if (
            fact["line_no"] != position
            or fact["journal_id"] != identifier
            or fact["group_version"] != (revision.group_version if revision else 1)
            or fact["revision_id"] != (revision.id if revision else None)
            or fact["payment_id"] != payment.id
            or fact["party_id"] != payment.party_id
            or fact["payment_asset_id"] != payment.asset_id
            or operation.kind != "unallocation"
            or operation.status != "active"
            or operation.current_journal_id != identifier
            or operation.version != fact["operation_version"]
            or operation.created_by != group.created_by
            or journal.operation_id != operation.id
            or journal.operation_version != operation.version
            or not journal.sealed
            or journal.journal_kind != "posting"
            or journal.reverses_journal_id is not None
            or journal.transaction_date != fact["return_date"]
            or journal.recognition_date != fact["return_date"]
            or journal.description != fact["return_reason"]
            or journal.revision_reason
            != (fact["revision_reason"] if operation.version > 1 else None)
            or len(lines) != (2 if fact["document_asset_id"] == payment.asset_id else 4)
        ):
            raise integrity()
        members.append(CurrentReturnMember(fact, operation, journal, tuple(lines)))
    return tuple(members)
