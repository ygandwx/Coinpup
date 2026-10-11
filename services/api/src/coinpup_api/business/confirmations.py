"""Atomic business confirmation; raw intent replay precedes current availability checks."""

from types import SimpleNamespace
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import func, insert, select

from coinpup_api.business.confirmation_hash import confirmation_hash_v2
from coinpup_api.business.confirmation_models import BusinessConfirmation, BusinessPosting
from coinpup_api.business.confirmation_plan import plan_confirmation
from coinpup_api.business.confirmation_schemas import (
    BusinessConfirmationResponse,
    ConfirmationPosting,
)
from coinpup_api.business.drafts import DraftService, _references, _response
from coinpup_api.business.models import BusinessDocument, BusinessParty, BusinessProject
from coinpup_api.ledger.balances import BalanceQueries
from coinpup_api.ledger.common import PostingCore, _invalid_money, asset_definition
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.idempotency import _IDEMPOTENCY_KEY, receipt_hash_version
from coinpup_api.ledger.models import (
    AssetRecord,
    Category,
    CommandReceipt,
    FinancialOperation,
    Journal,
    JournalLine,
)
from coinpup_api.ledger.period_checks import require_open_period
from coinpup_api.ledger.posting_storage import PostingLine, prepare_journal_lines
from coinpup_api.ledger.service import LedgerError, _not_found, _touch, _version


def _conflict():
    return LedgerError(
        "idempotency_conflict", 409, "This command key was used for a different request."
    )


def _replay(receipt, ledger_id, document_id, payload, digest):
    if receipt_hash_version(receipt) != 2 or receipt.request_hash != digest:
        raise _conflict()
    try:
        result = BusinessConfirmationResponse.model_validate(receipt.response)
        if (
            receipt.response_status != 201
            or result.id != payload.id
            or result.ledger_id != ledger_id
            or result.document_id != document_id
            or result.source_version != payload.expected_version
            or result.postings[0].operation_id != receipt.operation_id
        ):
            raise ValueError("Receipt identity mismatch")
    except (ValidationError, ValueError):
        raise LedgerError(
            "ledger_integrity", 503, "The stored command receipt is inconsistent."
        ) from None
    return result


class BusinessConfirmationService(BalanceQueries, PostingCore):
    def confirm(self, owner_id, ledger_id, document_id, payload, key, *, raw_body=None):
        if not isinstance(key, str) or _IDEMPOTENCY_KEY.fullmatch(key) is None:
            raise LedgerError(
                "invalid_idempotency_key",
                422,
                "Use 1–128 visible ASCII characters for the command key.",
            )
        with self._transaction(owner_id, write=True) as session:
            _, entity = self._locked_ledger(session, owner_id, ledger_id)
            receipt = session.get(CommandReceipt, (ledger_id, key))
            digest = confirmation_hash_v2(ledger_id, document_id, payload, raw_body=raw_body)
            if receipt is not None:
                return _replay(receipt, ledger_id, document_id, payload, digest)
            if entity.archived:
                raise LedgerError("entity_archived", 409, "Restore the entity before posting.")
            document = session.scalar(
                select(BusinessDocument).where(
                    BusinessDocument.id == document_id,
                    BusinessDocument.ledger_id == ledger_id,
                    BusinessDocument.document_kind.is_not(None),
                )
            )
            if document is None:
                raise _not_found()
            if document.state != "draft":
                raise LedgerError(
                    "document_confirmed", 409, "The document was already confirmed; reload it."
                )
            _version(document, payload.expected_version)
            if document.archived:
                raise LedgerError("document_archived", 409, "Restore the draft before confirming.")
            control_key = (
                "receivable.customer" if document.document_kind == "invoice" else "payable.supplier"
            )
            controls = ControlAccounts(self.engine).ensure(
                session, owner_id, ledger_id, {control_key: {document.asset_id}}
            )
            asset = asset_definition(session.get(AssetRecord, document.asset_id))
            sources = DraftService._lines(session, ledger_id, document_id)
            self._references(session, ledger_id, document, sources)
            try:
                plan = plan_confirmation(_response(document, sources, asset), asset)
                require_open_period(
                    session,
                    ledger_id,
                    *[
                        SimpleNamespace(
                            transaction_date=plan.transaction_date, recognition_date=day
                        )
                        for day in plan.recognition_dates
                    ],
                )
                groups = self._prepare(plan, asset, controls[control_key].id)
                self._validate_balance_deltas(
                    session,
                    ledger_id,
                    [row for _, _, _, rows, _ in groups for row in rows],
                    {asset.asset_id: asset},
                )
            except MoneyError as error:
                raise _invalid_money(error) from None
            return self._persist_group(
                session, owner_id, ledger_id, document, payload, key, digest, plan, groups
            )

    @staticmethod
    def _references(session, ledger_id, document, lines):
        party = _references(session, BusinessParty, ledger_id, {document.party_id})[
            document.party_id
        ]
        role = "customer" if document.document_kind == "invoice" else "supplier"
        if not party.name or party.role not in (role, "both"):
            raise LedgerError(
                "party_role", 422, "The party profile does not support this document."
            )
        categories = _references(session, Category, ledger_id, {row.category_id for row in lines})
        _references(
            session,
            BusinessProject,
            ledger_id,
            {row.project_id for row in lines if row.project_id is not None},
        )
        kind = "income" if document.document_kind == "invoice" else "expense"
        if any(row.kind != kind for row in categories.values()):
            raise LedgerError(
                "category_kind", 422, "The category kind does not match this document."
            )

    @staticmethod
    def _prepare(plan, asset, account_id):
        groups = []
        for source in plan.lines:
            operation, journal = uuid4(), uuid4()
            scope = dict(
                journal_id=journal,
                ledger_id=plan.ledger_id,
                asset_id=asset.asset_id,
                party_id=plan.party_id,
                document_id=plan.document_id,
                document_line_id=source.line_id,
                project_id=source.project_id,
            )
            rows = [
                PostingLine(
                    **scope,
                    line_no=1,
                    role="account",
                    account_id=account_id,
                    amount=source.account_amount,
                ),
                PostingLine(
                    **scope,
                    line_no=2,
                    role=plan.category_kind,
                    category_id=source.category_id,
                    amount=source.category_amount,
                ),
            ]
            groups.append(
                (
                    source,
                    operation,
                    journal,
                    rows,
                    prepare_journal_lines(rows, {asset.asset_id: asset}),
                )
            )
        return groups

    @staticmethod
    def _persist_group(session, owner_id, ledger_id, document, payload, key, digest, plan, groups):
        now = session.scalar(select(func.clock_timestamp()))
        session.execute(
            insert(BusinessConfirmation).values(
                id=payload.id,
                ledger_id=ledger_id,
                document_id=document.id,
                created_by=owner_id,
                source_version=payload.expected_version,
                idempotency_key=key,
                created_at=now,
            )
        )
        document.state = "confirmed"
        _touch(document)
        session.flush()
        members = []
        for source, operation, journal, _, parameters in groups:
            session.execute(
                insert(FinancialOperation).values(
                    id=operation,
                    ledger_id=ledger_id,
                    kind=plan.operation_kind,
                    version=1,
                    current_journal_id=journal,
                    created_by=owner_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.execute(
                insert(Journal).values(
                    id=journal,
                    operation_id=operation,
                    ledger_id=ledger_id,
                    operation_version=1,
                    journal_kind="posting",
                    transaction_date=plan.transaction_date,
                    recognition_date=source.recognition_date,
                    description=source.description,
                    created_at=now,
                )
            )
            session.execute(insert(JournalLine), parameters)
            session.execute(
                insert(BusinessPosting).values(
                    ledger_id=ledger_id,
                    confirmation_id=payload.id,
                    document_id=document.id,
                    line_id=source.line_id,
                    operation_id=operation,
                    journal_id=journal,
                    created_at=now,
                )
            )
            members.append(
                ConfirmationPosting(
                    line_id=source.line_id,
                    line_no=source.line_no,
                    operation_id=operation,
                    journal_id=journal,
                )
            )
        response = BusinessConfirmationResponse(
            kind="business_confirmation",
            id=payload.id,
            ledger_id=ledger_id,
            document_id=document.id,
            source_version=payload.expected_version,
            document_version=document.version,
            postings=tuple(members),
        )
        session.execute(
            insert(CommandReceipt).values(
                ledger_id=ledger_id,
                key=key,
                hash_version=2,
                request_hash=digest,
                response=response.model_dump(mode="json"),
                response_status=201,
                operation_id=members[0].operation_id,
                created_at=now,
            )
        )
        return response
