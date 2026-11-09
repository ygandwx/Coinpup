"""Atomic complete-group credit revisions with permanent original-intent receipts."""

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import func, insert, select

from coinpup_api.business.allocation_return_sources import require_current
from coinpup_api.business.confirmations import BusinessConfirmationService
from coinpup_api.business.credit_plan import CreditSource, plan_credit_line
from coinpup_api.business.credit_revision_current import read_current_credit
from coinpup_api.business.credit_revision_hash import credit_revision_hash_v2
from coinpup_api.business.credit_revision_member_models import BusinessCreditRevisionMember
from coinpup_api.business.credit_revision_models import BusinessCreditRevision
from coinpup_api.business.credit_revision_receipts import replay_revision, response_from_rows
from coinpup_api.business.credit_revision_rows import prepare_credit_revision_rows
from coinpup_api.business.credits import BusinessCreditService
from coinpup_api.business.settlement_totals import document_totals
from coinpup_api.ledger.balances import BalanceQueries
from coinpup_api.ledger.common import PostingCore, _invalid_money, asset_definition
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.idempotency import _IDEMPOTENCY_KEY
from coinpup_api.ledger.models import (
    AssetRecord,
    CommandReceipt,
    FinancialOperation,
    Journal,
    JournalLine,
)
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.period_checks import require_open_period
from coinpup_api.ledger.service import LedgerError, _not_found, _version


class BusinessCreditRevisionService(BalanceQueries, PostingCore):
    def correct(self, owner_id, ledger_id, credit_id, payload, key, *, raw_body=None):
        return self._revise(
            owner_id, ledger_id, credit_id, "correct", payload, key, raw_body=raw_body
        )

    def cancel(self, owner_id, ledger_id, credit_id, payload, key, *, raw_body=None):
        return self._revise(
            owner_id, ledger_id, credit_id, "cancel", payload, key, raw_body=raw_body
        )

    def _revise(self, owner_id, ledger_id, credit_id, action, payload, key, *, raw_body):
        if not isinstance(key, str) or _IDEMPOTENCY_KEY.fullmatch(key) is None:
            raise LedgerError("invalid_idempotency_key", 422, "Use 1–128 visible ASCII characters.")
        with self._transaction(owner_id, write=True, read_committed=True) as session:
            _, entity = self._locked_ledger(session, owner_id, ledger_id)
            digest = credit_revision_hash_v2(
                action, ledger_id, credit_id, payload, raw_body=raw_body
            )
            receipt = session.get(CommandReceipt, (ledger_id, key))
            if receipt is not None:
                return replay_revision(
                    session, receipt, ledger_id, credit_id, action, payload, digest
                )
            if entity.archived:
                raise LedgerError("entity_archived", 409, "Restore the entity before posting.")
            current = read_current_credit(session, ledger_id, credit_id)
            _version(SimpleNamespace(version=current.group_version), payload.expected_version)
            if current.cancelled:
                raise LedgerError("credit_cancelled", 409, "A cancelled credit cannot be changed.")
            if current.group_version >= 2147483647:
                raise LedgerError(
                    "credit_revision_version", 409, "The credit version cannot advance."
                )
            replacement = payload.replacement if action == "correct" else None
            document = current.document
            sources, controls = {}, {}
            assets = {line.asset_id for original in current.originals for line in original.lines}
            if replacement is not None:
                if document.state != "confirmed":
                    raise _not_found()
                _version(document, current.credit.source_version)
                if replacement.credit_date < document.issue_date:
                    raise LedgerError(
                        "credit_date", 422, "Credit cannot precede the original issue date."
                    )
                sources = BusinessCreditService._sources(session, ledger_id, document, replacement)
                BusinessConfirmationService._references(
                    session, ledger_id, document, [source[0] for source in sources.values()]
                )
                control_key = (
                    "receivable.customer"
                    if document.document_kind == "invoice"
                    else "payable.supplier"
                )
                assets.add(document.asset_id)
                # ensure owns the asset-lock step; never take an asset lock before this call.
                controls = ControlAccounts(self.engine).ensure(
                    session,
                    owner_id,
                    ledger_id,
                    {control_key: {document.asset_id}},
                    lock_assets=assets,
                )
            records = session.scalars(
                select(AssetRecord)
                .where(AssetRecord.asset_id.in_(assets))
                .order_by(AssetRecord.asset_id)
                .with_for_update(read=True)
            ).all()
            if len(records) != len(assets):
                raise _not_found()
            catalog = {record.asset_id: asset_definition(record) for record in records}
            require_open_period(
                session,
                ledger_id,
                *(item.journal for item in current.originals),
                *(
                    SimpleNamespace(
                        transaction_date=replacement.credit_date,
                        recognition_date=row.recognition_date,
                    )
                    for row in (replacement.lines if replacement else ())
                ),
            )
            try:
                plans = (
                    _plans(
                        session,
                        ledger_id,
                        current,
                        replacement,
                        sources,
                        catalog[document.asset_id],
                    )
                    if replacement is not None
                    else {}
                )
                groups = prepare_credit_revision_rows(
                    ledger_id,
                    credit_id,
                    document,
                    current.originals,
                    replacement=replacement,
                    plans=plans,
                    source_operations={
                        key: value[1].operation_id for key, value in sources.items()
                    },
                    account_id=controls[control_key].id if replacement is not None else None,
                    catalog=catalog,
                    reason=payload.reason,
                )
                self._validate_balance_deltas(
                    session,
                    ledger_id,
                    [
                        line
                        for item in groups
                        for journal in (item.reversal, item.replacement)
                        if journal
                        for line in journal.lines
                    ],
                    catalog,
                )
            except MoneyError as error:
                raise _invalid_money(error) from None
            now = session.scalar(select(func.clock_timestamp()))
            identifier = _persist(session, owner_id, current, payload, key, groups, now)
            result = response_from_rows(session, identifier)
            session.execute(
                insert(CommandReceipt).values(
                    ledger_id=ledger_id,
                    key=key,
                    hash_version=2,
                    request_hash=digest,
                    response=result.model_dump(mode="json"),
                    response_status=200,
                    operation_id=result.members[0].operation_id,
                    created_at=now,
                )
            )
            return result


def _plans(session, ledger_id, current, replacement, sources, asset):
    totals = document_totals(session, ledger_id, sources)
    old = {fact["source_line_id"]: fact for fact in current.facts}
    result = {}
    for requested in replacement.lines:
        row, original, operation = sources[requested.source_line_id]
        require_current(
            operation,
            1,
            original.journal_id,
            "invoice_accrual" if current.document.document_kind == "invoice" else "bill_accrual",
        )
        used, net, tax = (
            Amount.from_decimal(value, asset)
            for value in totals.get(row.id, (Decimal(0), Decimal(0), Decimal(0)))
        )
        previous = old.get(row.id, {})
        old_net, old_tax = (
            Amount.from_decimal(previous.get(field, Decimal(0)), asset)
            for field in ("net_amount", "tax_amount")
        )
        total = Amount.from_decimal(row.total_amount, asset)
        if (
            min(used.minor_units, old_net.minor_units, old_tax.minor_units) < 0
            or net < old_net
            or tax < old_tax
            or used + net + tax > total
        ):
            raise MoneyError("credit_integrity", "The stored source capacity is inconsistent.")
        source = CreditSource(
            ledger_id,
            current.document.id,
            row.id,
            current.document.document_kind,
            row.category_id,
            row.project_id,
            Amount.from_decimal(row.net_amount, asset),
            Amount.from_decimal(row.tax_amount, asset),
            total,
            net - old_net,
            tax - old_tax,
        )
        plan = plan_credit_line(ledger_id, source, requested.net_amount, requested.tax_amount)
        if plan.total > total - used - source.credited_net - source.credited_tax:
            raise MoneyError("credit_exceeds_open", "Release settled capacity before crediting.")
        result[row.id] = plan
    return result


def _persist(session, owner_id, current, payload, key, groups, now):
    identifier, ledger_id = uuid4(), current.credit.ledger_id
    replacement = getattr(payload, "replacement", None)
    session.execute(
        insert(BusinessCreditRevision).values(
            id=identifier,
            ledger_id=ledger_id,
            credit_id=current.credit.id,
            document_id=current.document.id,
            group_version=current.group_version + 1,
            previous_revision_id=current.revision.id if current.revision else None,
            action="correct" if replacement is not None else "cancel",
            revision_reason=payload.reason,
            credit_date=replacement.credit_date if replacement else None,
            credit_reason=replacement.reason if replacement else None,
            created_by=owner_id,
            idempotency_key=key,
            created_at=now,
        )
    )
    for position, item in enumerate(groups, 1):
        previous = item.change.previous
        if previous is None:
            session.execute(
                insert(FinancialOperation).values(
                    id=item.operation_id,
                    ledger_id=ledger_id,
                    kind=f"{current.document.document_kind}_credit",
                    version=1,
                    current_journal_id=item.replacement.header["id"],
                    created_by=owner_id,
                    created_at=now,
                    updated_at=now,
                )
            )
        for journal in (item.reversal, item.replacement):
            if journal is not None:
                session.execute(insert(Journal).values(**journal.header, created_at=now))
                session.execute(insert(JournalLine), list(journal.parameters))
        if previous is not None:
            operation = current.operations[item.operation_id]
            operation.version = item.operation_version
            operation.status = "active" if item.replacement else "cancelled"
            if item.replacement:
                operation.current_journal_id = item.replacement.header["id"]
            operation.updated_at = now
        session.execute(
            insert(BusinessCreditRevisionMember).values(
                id=uuid4(),
                ledger_id=ledger_id,
                revision_id=identifier,
                credit_id=current.credit.id,
                document_id=current.document.id,
                source_line_id=item.change.source_line_id,
                source_operation_id=item.source_operation_id,
                member_no=position,
                action=item.change.action,
                operation_id=item.operation_id,
                operation_version=item.operation_version,
                previous_line_no=previous.line_no if previous else None,
                previous_operation_version=previous.operation_version if previous else None,
                previous_journal_id=previous.journal_id if previous else None,
                reversal_journal_id=item.reversal.header["id"] if item.reversal else None,
                line_no=item.change.replacement_line_no,
                journal_id=item.replacement.header["id"] if item.replacement else None,
                net_amount=item.plan.net.to_decimal() if item.plan else None,
                tax_amount=item.plan.tax.to_decimal() if item.plan else None,
                total_amount=item.plan.total.to_decimal() if item.plan else None,
                recognition_date=item.replacement.header["recognition_date"]
                if item.replacement
                else None,
                created_at=now,
            )
        )
    session.flush()
    return identifier
