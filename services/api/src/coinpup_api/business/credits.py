"""Atomic manual credits, bounded original-line capacity and permanent v2 intentions."""

from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import func, insert, select

from coinpup_api.business.allocation_return_sources import require_current
from coinpup_api.business.confirmation_models import BusinessPosting
from coinpup_api.business.credit_hash import credit_hash_v2
from coinpup_api.business.credit_plan import CreditSource, plan_credit_line
from coinpup_api.business.credit_receipts import replay_credit, response_from_rows
from coinpup_api.business.credit_rows import credit_rows, persist_credit
from coinpup_api.business.models import BusinessDocument, BusinessDocumentLine
from coinpup_api.business.settlement_totals import document_totals
from coinpup_api.ledger.balances import BalanceQueries
from coinpup_api.ledger.common import PostingCore, _invalid_money, asset_definition
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.idempotency import _IDEMPOTENCY_KEY
from coinpup_api.ledger.models import AssetRecord, CommandReceipt, FinancialOperation
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.period_checks import require_open_period
from coinpup_api.ledger.service import LedgerError, _not_found, _version


class BusinessCreditService(BalanceQueries, PostingCore):
    def create(self, owner_id, ledger_id, payload, key, *, raw_body=None):
        if not isinstance(key, str) or _IDEMPOTENCY_KEY.fullmatch(key) is None:
            raise LedgerError("invalid_idempotency_key", 422, "Use 1–128 visible ASCII characters.")
        with self._transaction(owner_id, write=True, read_committed=True) as session:
            _, entity = self._locked_ledger(session, owner_id, ledger_id)
            digest = credit_hash_v2(ledger_id, payload, raw_body=raw_body)
            receipt = session.get(CommandReceipt, (ledger_id, key))
            if receipt is not None:
                return replay_credit(session, receipt, ledger_id, payload, digest)
            if entity.archived:
                raise LedgerError("entity_archived", 409, "Restore the entity before posting.")
            document = session.scalar(
                select(BusinessDocument).where(
                    BusinessDocument.id == payload.document_id,
                    BusinessDocument.ledger_id == ledger_id,
                    BusinessDocument.state == "confirmed",
                )
            )
            if document is None:
                raise _not_found()
            _version(document, payload.expected_version)
            if payload.credit_date < document.issue_date:
                raise LedgerError(
                    "credit_date", 422, "Credit cannot precede the original issue date."
                )
            sources = self._sources(session, ledger_id, document, payload)
            control_key = (
                "receivable.customer" if document.document_kind == "invoice" else "payable.supplier"
            )
            controls = ControlAccounts(self.engine).ensure(
                session,
                owner_id,
                ledger_id,
                {control_key: {document.asset_id}},
                lock_assets={document.asset_id},
            )
            asset = asset_definition(session.get(AssetRecord, document.asset_id))
            catalog = {asset.asset_id: asset}
            totals = document_totals(session, ledger_id, sources)
            groups = []
            try:
                for requested in payload.lines:
                    row, original, operation = sources[requested.source_line_id]
                    require_current(
                        operation,
                        1,
                        original.journal_id,
                        "invoice_accrual"
                        if document.document_kind == "invoice"
                        else "bill_accrual",
                    )
                    used, net, tax = (
                        Amount.from_decimal(value, asset)
                        for value in totals.get(row.id, (Decimal(0), Decimal(0), Decimal(0)))
                    )
                    source = CreditSource(
                        ledger_id,
                        document.id,
                        row.id,
                        document.document_kind,
                        row.category_id,
                        row.project_id,
                        Amount.from_decimal(row.net_amount, asset),
                        Amount.from_decimal(row.tax_amount, asset),
                        Amount.from_decimal(row.total_amount, asset),
                        net,
                        tax,
                    )
                    plan = plan_credit_line(
                        ledger_id, source, requested.net_amount, requested.tax_amount
                    )
                    if used.minor_units < 0 or used + net + tax > source.total:
                        raise MoneyError(
                            "credit_integrity", "The stored source capacity is inconsistent."
                        )
                    if plan.total > source.total - used - net - tax:
                        raise MoneyError(
                            "credit_exceeds_open", "Release settled capacity before crediting."
                        )
                    require_open_period(
                        session,
                        ledger_id,
                        SimpleNamespace(
                            transaction_date=payload.credit_date,
                            recognition_date=requested.recognition_date,
                        ),
                    )
                    group = credit_rows(
                        ledger_id, document, payload, plan, controls[control_key].id, catalog
                    )
                    groups.append((requested, original, plan, group))
                self._validate_balance_deltas(
                    session,
                    ledger_id,
                    [row for *_, group in groups for row in group.lines],
                    catalog,
                )
            except MoneyError as error:
                raise _invalid_money(error) from None
            now = session.scalar(select(func.clock_timestamp()))
            persist_credit(session, owner_id, ledger_id, payload, key, groups, now)
            result = response_from_rows(session, payload.id)
            session.execute(
                insert(CommandReceipt).values(
                    ledger_id=ledger_id,
                    key=key,
                    hash_version=2,
                    request_hash=digest,
                    response=result.model_dump(mode="json"),
                    response_status=201,
                    operation_id=result.postings[0].operation_id,
                    created_at=now,
                )
            )
            return result

    @staticmethod
    def _sources(session, ledger_id, document, payload):
        identifiers = {row.source_line_id for row in payload.lines}
        rows = session.execute(
            select(BusinessDocumentLine, BusinessPosting, FinancialOperation)
            .join(
                BusinessPosting,
                (BusinessPosting.line_id == BusinessDocumentLine.id)
                & (BusinessPosting.ledger_id == BusinessDocumentLine.ledger_id),
            )
            .join(
                FinancialOperation,
                (FinancialOperation.id == BusinessPosting.operation_id)
                & (FinancialOperation.ledger_id == BusinessPosting.ledger_id),
            )
            .where(
                BusinessDocumentLine.ledger_id == ledger_id,
                BusinessDocumentLine.document_id == document.id,
                BusinessPosting.document_id == document.id,
                BusinessDocumentLine.archived.is_(False),
                BusinessDocumentLine.id.in_(identifiers),
            )
        ).all()
        sources = {line.id: (line, original, operation) for line, original, operation in rows}
        if sources.keys() != identifiers:
            raise _not_found()
        return sources
