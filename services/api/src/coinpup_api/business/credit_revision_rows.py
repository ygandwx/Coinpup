"""Pure complete-group row assembly, without transactions, receipts or operation mutations."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from uuid import UUID, uuid4

from coinpup_api.business.credit_plan import CreditLinePlan
from coinpup_api.business.credit_revision_plan import (
    CreditMemberChange,
    CreditMemberIdentity,
    plan_credit_membership,
)
from coinpup_api.business.credit_revision_schemas import CreditReplacement
from coinpup_api.business.credit_rows import credit_rows, credit_rows_for_identity
from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import Journal, JournalLine
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting_storage import PostingLine, prepare_reversal_lines


@dataclass(frozen=True, slots=True)
class CreditRevisionOriginal:
    identity: CreditMemberIdentity
    source_operation_id: UUID
    journal: Journal
    lines: tuple[JournalLine, ...]


@dataclass(frozen=True, slots=True)
class CreditRevisionJournal:
    header: dict
    lines: tuple[PostingLine, ...]
    parameters: tuple[dict, ...]


@dataclass(frozen=True, slots=True)
class CreditRevisionMemberRows:
    change: CreditMemberChange
    source_operation_id: UUID
    operation_id: UUID
    operation_version: int
    reversal: CreditRevisionJournal | None
    replacement: CreditRevisionJournal | None
    plan: CreditLinePlan | None


def _invalid():
    return MoneyError(
        "credit_revision_integrity", "The credit revision row inputs are inconsistent."
    )


def _old_posting(ledger_id, original, catalog):
    identity, journal = original.identity, original.journal
    if (
        not isinstance(original.source_operation_id, UUID)
        or not isinstance(journal, Journal)
        or original.source_operation_id == identity.operation_id
        or (journal.id, journal.ledger_id, journal.operation_id, journal.operation_version)
        != (identity.journal_id, ledger_id, identity.operation_id, identity.operation_version)
        or journal.journal_kind != "posting"
        or journal.reverses_journal_id is not None
        or type(journal.transaction_date) is not date
        or type(journal.recognition_date) is not date
        or not isinstance(original.lines, tuple)
        or any(not isinstance(line, JournalLine) for line in original.lines)
        or any(
            line.journal_id != journal.id or line.ledger_id != ledger_id for line in original.lines
        )
        or any(line.asset_id not in catalog for line in original.lines)
    ):
        raise _invalid()


def _replacement_plan(document, requested, plan, catalog):
    asset = catalog.get(document.asset_id)
    if (
        not isinstance(plan, CreditLinePlan)
        or not isinstance(asset, AssetDefinition)
        or plan.document_id != document.id
        or plan.line_id != requested.source_line_id
        or not isinstance(plan.category_id, UUID)
        or (plan.project_id is not None and not isinstance(plan.project_id, UUID))
        or any(
            not isinstance(value, Amount) or value.asset != asset
            for value in (
                plan.net,
                plan.tax,
                plan.total,
                plan.category_amount,
                plan.control_amount,
            )
        )
    ):
        raise _invalid()
    invoice = document.document_kind == "invoice"
    if (
        plan.net != Amount.parse(requested.net_amount, asset)
        or plan.tax != Amount.parse(requested.tax_amount, asset)
        or plan.net.minor_units < 0
        or plan.tax.minor_units < 0
        or plan.total != plan.net + plan.tax
        or plan.total.minor_units <= 0
        or plan.category_role != ("income" if invoice else "expense")
        or plan.control_key != ("receivable.customer" if invoice else "payable.supplier")
        or plan.category_amount != (plan.total if invoice else -plan.total)
        or plan.control_amount != -plan.category_amount
    ):
        raise _invalid()


def prepare_credit_revision_rows(
    ledger_id,
    credit_id,
    document,
    current,
    *,
    replacement,
    plans,
    source_operations,
    account_id,
    catalog: Mapping[str, AssetDefinition],
    reason,
) -> tuple[CreditRevisionMemberRows, ...]:
    """Assemble exact rows after the caller has validated the locked financial facts.

    Current records must be the complete latest group and its complete posting lines.
    Replacement maps contain exactly its requested sources; source availability, original
    accrual shape, group version, dates/periods and capacity still require caller checks.
    No input operation/journal is changed. The caller supplies creation timestamps and
    validates all returned balance deltas once before an atomic write of the whole group.
    """
    if (
        not isinstance(current, tuple)
        or any(not isinstance(item, CreditRevisionOriginal) for item in current)
        or document.ledger_id != ledger_id
        or not isinstance(document.id, UUID)
        or document.document_kind not in {"invoice", "bill"}
        or not isinstance(reason, str)
        or not reason.strip()
        or reason != reason.strip()
        or len(reason) > 1000
        or "\x00" in reason
        or (replacement is not None and not isinstance(replacement, CreditReplacement))
    ):
        raise _invalid()
    requested = (
        () if replacement is None else tuple(item.source_line_id for item in replacement.lines)
    )
    changes = plan_credit_membership(
        ledger_id,
        credit_id,
        tuple(item.identity for item in current),
        None if replacement is None else requested,
    )
    if set(plans) != set(requested) or set(source_operations) != set(requested):
        raise _invalid()
    originals = {item.identity.source_line_id: item for item in current}
    for item in current:
        _old_posting(ledger_id, item, catalog)
    sources = {source: item.source_operation_id for source, item in originals.items()}
    for source, operation in source_operations.items():
        if not isinstance(operation, UUID) or (source in sources and sources[source] != operation):
            raise _invalid()
        sources[source] = operation
    if len(set(sources.values())) != len(sources) or set(sources.values()) & {
        item.identity.operation_id for item in current
    }:
        raise _invalid()
    if replacement is not None:
        if not isinstance(account_id, UUID):
            raise _invalid()
        for item in replacement.lines:
            _replacement_plan(document, item, plans[item.source_line_id], catalog)
    result = []
    for change in changes:
        reversal = new = plan = None
        operation = change.previous.operation_id if change.previous is not None else None
        version = change.next_operation_version
        if change.previous is not None:
            old = originals[change.source_line_id]
            identifier = uuid4()
            rows, parameters = prepare_reversal_lines(old.lines, identifier, catalog)
            reversal = CreditRevisionJournal(
                dict(
                    id=identifier,
                    ledger_id=ledger_id,
                    operation_id=operation,
                    operation_version=version,
                    journal_kind="reversal",
                    reverses_journal_id=old.journal.id,
                    transaction_date=old.journal.transaction_date,
                    recognition_date=old.journal.recognition_date,
                    description=old.journal.description,
                    revision_reason=reason,
                ),
                tuple(rows),
                tuple(parameters),
            )
        if change.action != "cancel":
            requested_line = replacement.lines[change.replacement_line_no - 1]
            plan = plans[change.source_line_id]
            args = (ledger_id, document, replacement, plan, account_id, catalog)
            group = (
                credit_rows(*args)
                if change.action == "create"
                else credit_rows_for_identity(
                    *args,
                    operation_id=operation,
                    journal_id=uuid4(),
                )
            )
            operation = group.operation_id
            new = CreditRevisionJournal(
                dict(
                    id=group.journal_id,
                    ledger_id=ledger_id,
                    operation_id=operation,
                    operation_version=version,
                    journal_kind="posting",
                    reverses_journal_id=None,
                    transaction_date=replacement.credit_date,
                    recognition_date=requested_line.recognition_date,
                    description=replacement.reason,
                    revision_reason=reason if change.action == "correct" else None,
                ),
                group.lines,
                group.parameters,
            )
        result.append(
            CreditRevisionMemberRows(
                change,
                sources[change.source_line_id],
                operation,
                version,
                reversal,
                new,
                plan,
            )
        )
    return tuple(result)
