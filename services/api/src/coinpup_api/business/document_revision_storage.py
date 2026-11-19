"""Persist one reviewed complete document group in the caller's locked transaction."""

from uuid import uuid4

from sqlalchemy import func, insert, select, update

from coinpup_api.business.document_fixed_receipts import (
    document_receipt_anchor,
    fixed_document_response,
)
from coinpup_api.business.document_revision_models import (
    BusinessDocumentAccrual,
    BusinessDocumentRevision,
    BusinessDocumentRevisionLine,
)
from coinpup_api.ledger.models import CommandReceipt, FinancialOperation, Journal, JournalLine
from coinpup_api.ledger.service import LedgerError


def persist_document_revision(
    session, owner_id, current, command, key, digest, references, journals, new_asset
):
    """Write prepared facts, operation heads and the exact immutable 200 without committing.

    The command service must authorize, lock, check the permanent key, reviewed references,
    periods, live dependencies and balance deltas before calling this function. Database
    deferred constraints prove the complete actual group when that transaction commits.
    """
    doc = current.document
    replacement = getattr(command, "replacement", None)
    identifier = uuid4()
    now = session.scalar(select(func.clock_timestamp()))
    shared = dict(
        ledger_id=doc.ledger_id,
        document_id=doc.id,
        confirmation_id=doc.confirmation_id,
        document_version=current.version + 1,
        created_at=now,
    )
    session.execute(
        insert(BusinessDocumentRevision).values(
            **shared,
            id=identifier,
            previous_revision_id=current.revision_id,
            previous_version=current.version,
            action="correct" if replacement is not None else "cancel",
            reason=command.reason,
            created_by=owner_id,
            idempotency_key=key,
            party_id=replacement.party_id if replacement else None,
            party_snapshot=references.party_snapshot if references else None,
            asset_id=replacement.asset_id if replacement else None,
            issue_date=replacement.issue_date if replacement else None,
            due_date=replacement.due_date if replacement else None,
            notes=replacement.notes if replacement else None,
        )
    )
    group = shared | dict(revision_id=identifier)
    fact_ids = {}
    if replacement is not None:
        prices = replacement.price(doc.document_kind, new_asset)
        by_source = {r.source_line_id: r for r in references.lines}
        facts = []
        for position, (line, price) in enumerate(
            zip(replacement.lines, prices.lines, strict=True), 1
        ):
            fact_id = uuid4()
            fact_ids[line.id] = fact_id
            reviewed = by_source[line.id]
            facts.append(
                group
                | line.model_dump(exclude={"id"})
                | dict(
                    id=fact_id,
                    source_line_id=line.id,
                    line_no=position,
                    category_kind="income" if doc.document_kind == "invoice" else "expense",
                    category_snapshot=reviewed.category_snapshot,
                    project_snapshot=reviewed.project_snapshot,
                    net_amount=price.net.to_decimal(),
                    tax_amount=price.tax.to_decimal(),
                    total_amount=price.total.to_decimal(),
                )
            )
        # The existing source trigger registers new stable identities in this same transaction.
        session.execute(insert(BusinessDocumentRevisionLine), facts)
    for member in journals.members:
        change = member.change
        action = change.posting_action
        if action is None:
            continue
        if action == "create":
            session.execute(
                insert(FinancialOperation).values(
                    id=member.operation_id,
                    ledger_id=doc.ledger_id,
                    kind=doc.document_kind + "_accrual",
                    version=1,
                    current_journal_id=member.replacement.header["id"],
                    created_by=owner_id,
                    created_at=now,
                    updated_at=now,
                )
            )
        for journal in (member.reversal, member.replacement):
            if journal is not None:
                session.execute(insert(Journal).values(**journal.header, created_at=now))
                session.execute(insert(JournalLine), list(journal.parameters))
        prior = change.previous_posting
        if prior is not None:
            result = session.execute(
                update(FinancialOperation)
                .where(
                    FinancialOperation.id == prior.operation_id,
                    FinancialOperation.ledger_id == doc.ledger_id,
                    FinancialOperation.version == prior.version,
                    FinancialOperation.current_journal_id == prior.journal_id,
                    FinancialOperation.status == "active",
                )
                .values(
                    version=member.operation_version,
                    status="active" if member.replacement else "cancelled",
                    current_journal_id=member.replacement.header["id"]
                    if member.replacement
                    else prior.journal_id,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise LedgerError(
                    "document_revision_integrity", 409, "The sealed operation head changed."
                )
        session.execute(
            insert(BusinessDocumentAccrual).values(
                **group,
                id=uuid4(),
                source_line_id=change.source_line_id,
                replacement_fact_id=fact_ids[change.source_line_id] if member.replacement else None,
                action=action,
                operation_id=member.operation_id,
                operation_version=member.operation_version,
                previous_journal_id=prior.journal_id if prior else None,
                reversal_journal_id=member.reversal.header["id"] if member.reversal else None,
                journal_id=member.replacement.header["id"] if member.replacement else None,
            )
        )
    result = fixed_document_response(session, doc.ledger_id, doc.id, identifier)
    session.execute(
        insert(CommandReceipt).values(
            ledger_id=doc.ledger_id,
            key=key,
            hash_version=2,
            request_hash=digest,
            response_status=200,
            response=result.model_dump(mode="json"),
            operation_id=document_receipt_anchor(result),
            created_at=now,
        )
    )
    return result
