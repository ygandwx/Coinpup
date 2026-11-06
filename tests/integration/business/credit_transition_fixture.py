"""Fictional complete pending transitions; callers must roll back while the gate is closed."""

from datetime import date
from uuid import uuid4

from coinpup_api.business.credit_revision_member_models import (
    BusinessCreditRevisionMember as Member,
)
from coinpup_api.business.credit_revision_models import BusinessCreditRevision as Revision
from coinpup_api.ledger.models import FinancialOperation
from sqlalchemy import insert, update

from tests.integration.business.test_credit_fact_validation import copy_posting, lock_credit, stored
from tests.integration.business.test_credit_service import credit, setup_credit


def group(s, *, kind="invoice", count=3, selected=2):
    document, _, confirmed, request = setup_credit(s, kind, count=count)
    request = request.model_copy(update={"lines": request.lines[:selected]})
    original = credit(s, request)
    return dict(
        original=original,
        request=request,
        source_operations={row.line_id: row.operation_id for row in confirmed.postings},
        source_ids=[row.id for row in document.lines],
        current={
            row.source_line_id: row.model_dump() | {"operation_version": 1}
            for row in original.postings
        },
        version=1,
        previous=None,
        owner=s["owner"],
    )


def transition(
    connection,
    state,
    sources,
    *,
    header_changes=None,
    member_changes=None,
    journal_changes=None,
    posting_line_changes=None,
    keep_head=False,
):
    original, old = state["original"], state["current"]
    lock_credit(connection, original)
    version = state["version"] + 1
    header = dict(
        id=uuid4(),
        credit_id=original.id,
        ledger_id=original.ledger_id,
        document_id=original.document_id,
        group_version=version,
        previous_revision_id=state["previous"],
        action="cancel" if sources is None else "correct",
        revision_reason=f"Fictional group revision {version}",
        credit_date=date(2026, 11, 6) if sources is not None else None,
        credit_reason="Fictional replacement 中文" if sources is not None else None,
        created_by=state["owner"],
        idempotency_key=str(uuid4()),
    ) | (header_changes or {})
    connection.execute(insert(Revision).values(**header))
    requested = sources or []
    order = list(requested) + [source for source in old if source not in requested]
    created = []
    for position, source in enumerate(order, 1):
        prior = old.get(source)
        action = "cancel" if source not in requested else "correct" if prior else "create"
        template = prior or original.postings[0].model_dump()
        operation = prior["operation_id"] if prior else uuid4()
        operation_version = prior["operation_version"] + 1 if prior else 1
        journal = None if action == "cancel" else uuid4()
        reversal = uuid4() if prior else None
        row = dict(
            id=uuid4(),
            ledger_id=original.ledger_id,
            revision_id=header["id"],
            credit_id=original.id,
            document_id=original.document_id,
            member_no=position,
            action=action,
            source_line_id=source,
            source_operation_id=state["source_operations"][source],
            operation_id=operation,
            operation_version=operation_version,
            previous_line_no=prior["line_no"] if prior else None,
            previous_operation_version=prior["operation_version"] if prior else None,
            previous_journal_id=prior["journal_id"] if prior else None,
            reversal_journal_id=reversal,
            line_no=position if action != "cancel" else None,
            journal_id=journal,
            net_amount=template["net_amount"] if journal else None,
            tax_amount=template["tax_amount"] if journal else None,
            total_amount=template["total_amount"] if journal else None,
            recognition_date=date(2026, 11, 6) if journal else None,
        )
        if prior:
            if not (keep_head(row) if callable(keep_head) else keep_head):
                connection.execute(
                    update(FinancialOperation)
                    .where(FinancialOperation.id == operation)
                    .values(
                        version=operation_version,
                        status="cancelled" if action == "cancel" else "active",
                        current_journal_id=journal or prior["journal_id"],
                    )
                )
        else:
            born = stored(connection, FinancialOperation, original.postings[0].operation_id)
            connection.execute(
                insert(FinancialOperation).values(
                    **(
                        born
                        | dict(id=operation, version=1, status="active", current_journal_id=journal)
                    )
                )
            )
        if journal:
            copy_posting(
                connection,
                template["journal_id"],
                dict(
                    id=journal,
                    operation_id=operation,
                    operation_version=operation_version,
                    transaction_date=header["credit_date"],
                    recognition_date=row["recognition_date"],
                    description=header["credit_reason"],
                    revision_reason=header["revision_reason"] if prior else None,
                ),
                line_changes={"document_line_id": source} | (posting_line_changes or {}),
            )
        if prior:
            copy_posting(
                connection,
                prior["journal_id"],
                dict(
                    id=reversal,
                    operation_version=operation_version,
                    journal_kind="reversal",
                    reverses_journal_id=prior["journal_id"],
                    revision_reason=header["revision_reason"],
                )
                | ((journal_changes(row) if callable(journal_changes) else journal_changes) or {}),
                reverse=True,
            )
        if member_changes is not None:
            row |= member_changes(row)
        if row.pop("omit", False) is False:
            connection.execute(insert(Member).values(**row))
        created.append(row)
    state.update(
        version=header["group_version"],
        previous=header["id"],
        current={row["source_line_id"]: row for row in created if row["action"] != "cancel"},
    )
    return header, created
