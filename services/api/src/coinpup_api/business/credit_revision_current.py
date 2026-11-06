"""Bounded current-group inputs for a caller-owned, authorized consistent transaction."""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from coinpup_api.business.credit_models import BusinessCredit
from coinpup_api.business.credit_projections import posting_facts
from coinpup_api.business.credit_revision_models import BusinessCreditRevision
from coinpup_api.business.credit_revision_plan import CreditMemberIdentity
from coinpup_api.business.credit_revision_receipts import integrity
from coinpup_api.business.credit_revision_rows import CreditRevisionOriginal
from coinpup_api.business.models import BusinessDocument
from coinpup_api.ledger.models import FinancialOperation, Journal, JournalLine
from coinpup_api.ledger.service import _not_found


@dataclass(frozen=True, slots=True)
class CreditRevisionCurrent:
    credit: BusinessCredit
    document: BusinessDocument
    revision: BusinessCreditRevision | None
    facts: tuple[Mapping, ...]
    originals: tuple[CreditRevisionOriginal, ...]
    operations: Mapping[UUID, FinancialOperation]

    @property
    def group_version(self):
        return 1 if self.revision is None else self.revision.group_version

    @property
    def cancelled(self):
        return self.revision is not None and self.revision.action == "cancel"


def read_current_credit(session, ledger_id, credit_id):
    """Read at most 200 complete members in five SELECTs (three for a cancelled group).

    The caller authenticates and owns the consistent snapshot: a write caller first
    takes the advisory, ledger and entity locks. This helper never acquires locks,
    flushes, commits, checks today's reference availability or replays a receipt.
    It assembles committed group facts, not a substitute for lifecycle validation.
    New commands separately check expected_version, terminal state, periods and capacity.
    """
    with session.no_autoflush:
        return _read(session, ledger_id, credit_id)


def _read(session, ledger_id, credit_id):
    saved = session.execute(
        select(BusinessCredit, BusinessDocument)
        .join(
            BusinessDocument,
            (BusinessDocument.id == BusinessCredit.document_id)
            & (BusinessDocument.ledger_id == BusinessCredit.ledger_id),
        )
        .where(BusinessCredit.id == credit_id, BusinessCredit.ledger_id == ledger_id)
    ).one_or_none()
    if saved is None:
        raise _not_found()
    credit, document = saved
    revision = session.scalar(
        select(BusinessCreditRevision)
        .where(
            BusinessCreditRevision.credit_id == credit_id,
            BusinessCreditRevision.ledger_id == ledger_id,
        )
        .order_by(BusinessCreditRevision.group_version.desc())
        .limit(1)
    )
    version = 1 if revision is None else revision.group_version
    if revision is not None and (
        revision.document_id != document.id
        or revision.action not in {"correct", "cancel"}
        or version < 2
    ):
        raise integrity()
    relation = posting_facts(ledger_id, credit_ids=[credit_id])
    facts = tuple(
        session.execute(
            select(relation)
            .where(relation.c.group_version == version)
            .order_by(relation.c.line_no)
            .limit(201)
        ).mappings()
    )
    if revision is not None and revision.action == "cancel":
        if facts:
            raise integrity()
        return CreditRevisionCurrent(credit, document, revision, (), (), {})
    if not 1 <= len(facts) <= 200:
        raise integrity()
    expected_date = credit.credit_date if revision is None else revision.credit_date
    expected_reason = credit.reason if revision is None else revision.credit_reason
    for position, fact in enumerate(facts, 1):
        if (
            (fact["ledger_id"], fact["credit_id"], fact["document_id"], fact["group_version"])
            != (ledger_id, credit_id, document.id, version)
            or fact["revision_id"] != (None if revision is None else revision.id)
            or fact["line_no"] != position
            or fact["credit_date"] != expected_date
            or fact["credit_reason"] != expected_reason
        ):
            raise integrity()
    for field in ("source_line_id", "source_operation_id", "operation_id", "journal_id"):
        if len({fact[field] for fact in facts}) != len(facts):
            raise integrity()
    rows = session.execute(
        select(FinancialOperation, Journal)
        .join(
            Journal,
            (Journal.id == FinancialOperation.current_journal_id)
            & (Journal.ledger_id == FinancialOperation.ledger_id),
        )
        .where(
            FinancialOperation.ledger_id == ledger_id,
            FinancialOperation.id.in_([fact["operation_id"] for fact in facts]),
        )
    ).all()
    records = {operation.id: (operation, journal) for operation, journal in rows}
    if len(records) != len(facts):
        raise integrity()
    identifiers = {fact["journal_id"] for fact in facts}
    lines = session.scalars(
        select(JournalLine)
        .where(JournalLine.ledger_id == ledger_id, JournalLine.journal_id.in_(identifiers))
        .order_by(JournalLine.journal_id, JournalLine.line_no)
        .limit(401)
    ).all()
    if len(lines) != 2 * len(facts):
        raise integrity()
    by_journal = {identifier: [] for identifier in identifiers}
    for line in lines:
        by_journal[line.journal_id].append(line)
    originals = []
    for fact in facts:
        operation, journal = records[fact["operation_id"]]
        if (
            operation.status != "active"
            or operation.kind != f"{document.document_kind}_credit"
            or operation.created_by != credit.created_by
            or operation.version != fact["operation_version"]
            or (journal.id, journal.operation_id, journal.operation_version)
            != (fact["journal_id"], operation.id, operation.version)
            or journal.journal_kind != "posting"
            or journal.reverses_journal_id is not None
            or journal.transaction_date != fact["credit_date"]
            or journal.recognition_date != fact["recognition_date"]
            or journal.description != fact["credit_reason"]
            or journal.revision_reason != fact["posting_revision_reason"]
            or [line.line_no for line in by_journal[journal.id]] != [1, 2]
        ):
            raise integrity()
        identity = CreditMemberIdentity(
            ledger_id,
            credit_id,
            fact["line_no"],
            fact["source_line_id"],
            operation.id,
            operation.version,
            journal.id,
        )
        originals.append(
            CreditRevisionOriginal(
                identity, fact["source_operation_id"], journal, tuple(by_journal[journal.id])
            )
        )
    return CreditRevisionCurrent(
        credit,
        document,
        revision,
        facts,
        tuple(originals),
        {identifier: row[0] for identifier, row in records.items()},
    )
