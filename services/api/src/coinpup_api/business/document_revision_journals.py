"""Prepare complete accrual reversals and replacements from a locked, sealed document version."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select

from coinpup_api.business.confirmation_reads import BusinessConfirmedDocument
from coinpup_api.business.confirmations import accrual_posting_lines
from coinpup_api.business.document_revision_plan import (
    DocumentLineTransition,
    SealedAccrual,
    plan_document_accrual_revision,
)
from coinpup_api.business.document_revision_schemas import (
    ConfirmedDocumentCancellation,
    ConfirmedDocumentCorrection,
)
from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import (
    Account,
    AccountAsset,
    AssetRecord,
    FinancialOperation,
    Journal,
    JournalLine,
)
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting_storage import PreparedJournal, prepare_reversal_lines
from coinpup_api.ledger.service import _version


@dataclass(frozen=True, slots=True)
class DocumentAccrualMember:
    change: DocumentLineTransition
    operation_id: UUID | None
    operation_version: int | None
    reversal: PreparedJournal | None
    replacement: PreparedJournal | None


@dataclass(frozen=True, slots=True)
class DocumentAccrualJournals:
    affected_dates: tuple[date, ...]
    members: tuple[DocumentAccrualMember, ...]


def _inconsistent():
    return MoneyError("document_revision_integrity", "The sealed accrual is inconsistent.")


def _posting_rows(session, ledger, postings):
    # A confirmed accrual has two lines. One scoped read obtains all their dimensions and
    # the actual operation head. The caller already holds the write lock for this ledger.
    identifiers = [p.journal_id for p in postings]
    rows = session.execute(
        select(Journal, FinancialOperation, JournalLine, Account.system_key)
        .select_from(Journal)
        .join(
            FinancialOperation,
            (FinancialOperation.id == Journal.operation_id)
            & (FinancialOperation.ledger_id == Journal.ledger_id),
        )
        .join(
            JournalLine,
            (JournalLine.journal_id == Journal.id) & (JournalLine.ledger_id == Journal.ledger_id),
        )
        .outerjoin(
            Account,
            (Account.id == JournalLine.account_id) & (Account.ledger_id == Journal.ledger_id),
        )
        .where(Journal.ledger_id == ledger, Journal.id.in_(identifiers))
        .order_by(Journal.id, JournalLine.line_no)
        .limit(2 * len(identifiers) + 1)
    ).all()
    if len(rows) != 2 * len(identifiers):
        raise _inconsistent()
    by_journal = defaultdict(list)
    for row in rows:
        by_journal[row[0].id].append(row)
    return by_journal


def _reverse(document, change, rows, asset, reason):
    old, source = change.previous_posting, change.previous
    if len(rows) != 2:
        raise _inconsistent()
    journal, operation, _, _ = rows[0]
    expected_key = (
        "receivable.customer" if document.document_kind == "invoice" else "payable.supplier"
    )
    if (
        (operation.id, operation.version, operation.current_journal_id)
        != (old.operation_id, old.version, old.journal_id)
        or operation.status != "active"
        or operation.kind != document.document_kind + "_accrual"
        or (
            journal.journal_kind,
            journal.sealed,
            journal.operation_version,
            journal.reverses_journal_id,
        )
        != ("posting", True, old.version, None)
        or (journal.transaction_date, journal.recognition_date, journal.description)
        != (document.issue_date, source.recognition_date, source.description)
    ):
        raise _inconsistent()
    by_role = {line.role: (line, control_key) for _, _, line, control_key in rows}
    if set(by_role) != {"account", source.category_kind}:
        raise _inconsistent()
    account, control_key = by_role["account"]
    category, _ = by_role[source.category_kind]
    if control_key != expected_key or category.category_id != source.category_id:
        raise _inconsistent()
    amount = Amount.from_decimal(Decimal(source.total_amount), asset)
    debit = amount if document.document_kind == "invoice" else -amount
    for line, signed in ((account, debit), (category, -debit)):
        if (
            line.ledger_id,
            line.asset_id,
            line.document_id,
            line.document_line_id,
            line.party_id,
            line.project_id,
            line.component_no,
            line.counterparty_entity_id,
            line.dimension_owner_id,
        ) != (
            document.ledger_id,
            document.asset_id,
            document.id,
            source.id,
            document.party_id,
            source.project_id,
            0,
            None,
            None,
        ) or Amount.from_decimal(line.amount, asset) != signed:
            raise _inconsistent()
    identifier = uuid4()
    lines, params = prepare_reversal_lines(
        [row[2] for row in rows], identifier, {asset.asset_id: asset}
    )
    return PreparedJournal(
        dict(
            id=identifier,
            ledger_id=document.ledger_id,
            operation_id=old.operation_id,
            operation_version=old.version + 1,
            journal_kind="reversal",
            reverses_journal_id=journal.id,
            transaction_date=journal.transaction_date,
            recognition_date=journal.recognition_date,
            description=journal.description,
            revision_reason=reason,
        ),
        tuple(lines),
        tuple(params),
    )


def prepare_document_revision_journals(
    session,
    current: BusinessConfirmedDocument,
    command: ConfirmedDocumentCorrection | ConfirmedDocumentCancellation,
    postings: tuple[SealedAccrual, ...],
    old_asset: AssetDefinition,
    new_asset: AssetDefinition | None,
) -> DocumentAccrualJournals:
    """Read the actual postings and enabled replacement control in the caller's transaction."""
    if not isinstance(command, (ConfirmedDocumentCorrection, ConfirmedDocumentCancellation)):
        raise _inconsistent()
    _version(current, command.expected_version)
    replacement = command.replacement if isinstance(command, ConfirmedDocumentCorrection) else None
    plan = plan_document_accrual_revision(current, old_asset, postings, replacement, new_asset)
    account_id = None
    if replacement is not None:
        key = "receivable.customer" if current.document_kind == "invoice" else "payable.supplier"
        account_id = session.scalar(
            select(Account.id)
            .join(
                AccountAsset,
                (AccountAsset.account_id == Account.id)
                & (AccountAsset.ledger_id == Account.ledger_id),
            )
            .join(AssetRecord, AssetRecord.asset_id == AccountAsset.asset_id)
            .where(
                Account.ledger_id == current.ledger_id,
                Account.system_key == key,
                Account.archived.is_(False),
                AccountAsset.enabled.is_(True),
                AssetRecord.enabled.is_(True),
                AssetRecord.asset_id == new_asset.asset_id,
                AssetRecord.scale == new_asset.scale,
            )
        )
        if account_id is None:
            raise _inconsistent()
    sealed = _posting_rows(session, current.ledger_id, postings)
    kind = "income" if current.document_kind == "invoice" else "expense"
    members = []
    for change in plan.lines:
        reversal = new = operation = None
        version = change.next_operation_version
        if change.previous_posting is not None:
            old = change.previous_posting
            operation = old.operation_id
            reversal = _reverse(current, change, sealed[old.journal_id], old_asset, command.reason)
        if change.accrual is not None:
            operation = operation or uuid4()
            identifier = uuid4()
            rows, params = accrual_posting_lines(
                change.accrual,
                identifier,
                current.ledger_id,
                current.id,
                replacement.party_id,
                kind,
                new_asset,
                account_id,
            )
            new = PreparedJournal(
                dict(
                    id=identifier,
                    ledger_id=current.ledger_id,
                    operation_id=operation,
                    operation_version=version,
                    journal_kind="posting",
                    transaction_date=replacement.issue_date,
                    recognition_date=change.accrual.recognition_date,
                    description=change.accrual.description,
                    revision_reason=command.reason if version > 1 else None,
                ),
                tuple(rows),
                tuple(params),
            )
        members.append(DocumentAccrualMember(change, operation, version, reversal, new))
    return DocumentAccrualJournals(plan.affected_dates, tuple(members))
