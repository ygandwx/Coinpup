"""Atomic payment and allocation commands with permanent original-intent receipts."""

from types import SimpleNamespace

from pydantic import ValidationError
from sqlalchemy import func, insert, select

from coinpup_api.business.drafts import _references
from coinpup_api.business.models import BusinessDocument, BusinessDocumentLine, BusinessParty
from coinpup_api.business.settlement_hash import allocation_hash_v2, payment_hash_v2
from coinpup_api.business.settlement_models import BusinessAllocationLine, BusinessPayment
from coinpup_api.business.settlement_plan import (
    AllocationSource,
    PaymentSource,
    plan_allocation,
    plan_payment,
)
from coinpup_api.business.settlement_rows import (
    allocation_rows,
    payment_rows,
    persist_allocation,
    persist_posting,
)
from coinpup_api.business.settlement_schemas import (
    BusinessAllocationCreate,
    BusinessAllocationResponse,
    BusinessPaymentResponse,
)
from coinpup_api.ledger.balances import BalanceQueries
from coinpup_api.ledger.commands.fees import FeeCommands
from coinpup_api.ledger.common import PostingCore, _invalid_money, asset_definition
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.idempotency import _IDEMPOTENCY_KEY, receipt_hash_version
from coinpup_api.ledger.models import AssetRecord, CommandReceipt
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.period_checks import require_open_period
from coinpup_api.ledger.service import LedgerError, _not_found


def replay(receipt, ledger_id, payload, digest, payment):
    if receipt_hash_version(receipt) != 2 or receipt.request_hash != digest:
        raise LedgerError("idempotency_conflict", 409, "This key belongs to a different request.")
    try:
        schema = BusinessPaymentResponse if payment else BusinessAllocationResponse
        result = schema.model_validate(receipt.response)
        anchor = result.operation_id if payment else result.postings[0].operation_id
        if (
            result.id != payload.id
            or result.ledger_id != ledger_id
            or receipt.response_status != 201
            or receipt.operation_id != anchor
        ):
            raise ValueError("Receipt identity mismatch")
        group = result.initial_allocation if payment else result
        intention = payload.initial_allocation if payment else payload
        if (group is None) != (intention is None):
            raise ValueError("Receipt membership mismatch")
        if group is not None and (
            group.id != intention.id
            or group.payment_id != (payload.id if payment else payload.payment_id)
            or [(x.document_id, x.document_line_id) for x in group.postings]
            != [(x.document_id, x.document_line_id) for x in intention.lines]
        ):
            raise ValueError("Receipt membership mismatch")
    except (ValidationError, ValueError):
        raise LedgerError("ledger_integrity", 503, "The stored receipt is inconsistent.") from None
    return result


def source_rows(session, ledger_id, payload):
    if payload is None:
        return {}
    identifiers = {line.document_line_id for line in payload.lines}
    records = session.execute(
        select(BusinessDocument, BusinessDocumentLine)
        .join(
            BusinessDocumentLine,
            BusinessDocumentLine.document_id == BusinessDocument.id,
        )
        .where(
            BusinessDocument.ledger_id == ledger_id,
            BusinessDocumentLine.ledger_id == ledger_id,
            BusinessDocumentLine.id.in_(identifiers),
            BusinessDocument.state == "confirmed",
        )
    ).all()
    sources = {line.id: (document, line) for document, line in records}
    if sources.keys() != identifiers:
        raise _not_found()
    return sources


def allocation_plan(session, ledger_id, payment, payload, sources, catalog, *, new_payment):
    asset = catalog[payment.asset_id]
    applied = (
        None
        if new_payment
        else session.scalar(
            select(func.sum(BusinessAllocationLine.payment_amount)).where(
                BusinessAllocationLine.payment_id == payment.id,
                BusinessAllocationLine.ledger_id == ledger_id,
            )
        )
    )
    amount = (
        Amount.parse(payment.amount, asset)
        if new_payment
        else Amount.from_decimal(payment.amount, asset)
    )
    payer = PaymentSource(
        payment.id,
        ledger_id,
        payment.party_id,
        payment.direction,
        payment.transaction_date,
        amount,
        Amount(asset, 0) if applied is None else Amount.from_decimal(applied, asset),
    )
    totals = dict(
        session.execute(
            select(
                BusinessAllocationLine.document_line_id,
                func.sum(BusinessAllocationLine.document_amount),
            )
            .where(
                BusinessAllocationLine.document_line_id.in_(sources),
                BusinessAllocationLine.ledger_id == ledger_id,
            )
            .group_by(BusinessAllocationLine.document_line_id)
        ).all()
    )
    inputs = {}
    for identifier, (document, line) in sources.items():
        definition = catalog[document.asset_id]
        total = totals.get(identifier)
        inputs[identifier] = AllocationSource(
            document.id,
            line.id,
            ledger_id,
            document.party_id,
            document.document_kind,
            document.issue_date,
            Amount.from_decimal(line.total_amount, definition),
            Amount(definition, 0) if total is None else Amount.from_decimal(total, definition),
            line.project_id,
        )
    request = (
        payload
        if not new_payment
        else BusinessAllocationCreate(**payload.model_dump(), payment_id=payment.id)
    )
    return plan_allocation(ledger_id, payer, request, inputs, catalog)


class BusinessSettlementService(FeeCommands, BalanceQueries, PostingCore):
    def __init__(self, engine):
        # Scope the default to these commands, before the first owner/lock query.
        super().__init__(
            None if engine is None else engine.execution_options(isolation_level="READ COMMITTED")
        )

    def pay(self, owner_id, ledger_id, payload, key, *, raw_body=None):
        return self._execute(owner_id, ledger_id, payload, key, raw_body, payment=True)

    def allocate(self, owner_id, ledger_id, payload, key, *, raw_body=None):
        return self._execute(owner_id, ledger_id, payload, key, raw_body, payment=False)

    def _execute(self, owner_id, ledger_id, payload, key, raw_body, *, payment):
        if not isinstance(key, str) or _IDEMPOTENCY_KEY.fullmatch(key) is None:
            raise LedgerError("invalid_idempotency_key", 422, "Use 1–128 visible ASCII characters.")
        with self._transaction(owner_id, write=True) as session:
            _, entity = self._locked_ledger(session, owner_id, ledger_id)
            receipt = session.get(CommandReceipt, (ledger_id, key))
            digest = (payment_hash_v2 if payment else allocation_hash_v2)(
                ledger_id, payload, raw_body=raw_body
            )
            if receipt is not None:
                return replay(receipt, ledger_id, payload, digest, payment)
            if entity.archived:
                raise LedgerError("entity_archived", 409, "Restore the entity before posting.")
            payer = (
                payload
                if payment
                else session.scalar(
                    select(BusinessPayment).where(
                        BusinessPayment.id == payload.payment_id,
                        BusinessPayment.ledger_id == ledger_id,
                    )
                )
            )
            if payer is None:
                raise _not_found()
            request = payload.initial_allocation if payment else payload
            sources = source_rows(session, ledger_id, request)
            advance = "advance.received" if payer.direction == "receipt" else "advance.paid"
            control = "receivable.customer" if payer.direction == "receipt" else "payable.supplier"
            requirements = {advance: {payer.asset_id}}
            if sources:
                requirements[control] = {document.asset_id for document, _ in sources.values()}
            assets = set().union(*requirements.values()) | (
                {f.asset_id for f in payload.fees} if payment else set()
            )
            controls = ControlAccounts(self.engine).ensure(
                session, owner_id, ledger_id, requirements, lock_assets=assets
            )
            catalog = {
                row.asset_id: asset_definition(row)
                for row in session.scalars(
                    select(AssetRecord).where(AssetRecord.asset_id.in_(assets))
                )
            }
            if payment:
                party = _references(session, BusinessParty, ledger_id, {payload.party_id})[
                    payload.party_id
                ]
                role = "customer" if payload.direction == "receipt" else "supplier"
                if not party.name or party.role not in (role, "both"):
                    raise LedgerError(
                        "party_role", 422, "The party profile does not support this payment."
                    )
                self._account(session, ledger_id, payload.account_id, payload.asset_id)
            try:
                principal = (
                    None
                    if not payment
                    else payment_rows(
                        self,
                        session,
                        ledger_id,
                        payload,
                        plan_payment(payload, catalog),
                        controls,
                        catalog,
                    )
                )
                plan = (
                    None
                    if request is None
                    else allocation_plan(
                        session, ledger_id, payer, request, sources, catalog, new_payment=payment
                    )
                )
                groups = (
                    ()
                    if plan is None
                    else allocation_rows(
                        ledger_id, payer.party_id, plan, controls, sources, catalog
                    )
                )
                days = ([payload.transaction_date] if payment else []) + (
                    [request.allocation_date] if request else []
                )
                require_open_period(
                    session,
                    ledger_id,
                    *[SimpleNamespace(transaction_date=day, recognition_date=day) for day in days],
                )
                prepared = ([principal] if principal else []) + list(groups)
                self._validate_balance_deltas(
                    session,
                    ledger_id,
                    [line for group in prepared for line in group.lines],
                    catalog,
                )
            except MoneyError as error:
                raise _invalid_money(error) from None
            now = session.scalar(select(func.clock_timestamp()))
            if payment:
                session.execute(
                    insert(BusinessPayment).values(
                        id=payload.id,
                        ledger_id=ledger_id,
                        party_id=payload.party_id,
                        account_id=payload.account_id,
                        asset_id=payload.asset_id,
                        direction=payload.direction,
                        amount=Amount.parse(payload.amount, catalog[payload.asset_id]).to_decimal(),
                        transaction_date=payload.transaction_date,
                        description=payload.description,
                        fees=[f.model_dump(mode="json") for f in payload.fees],
                        operation_id=principal.operation_id,
                        journal_id=principal.journal_id,
                        initial_allocation_id=request.id if request else None,
                        created_by=owner_id,
                        idempotency_key=key,
                        created_at=now,
                    )
                )
                persist_posting(
                    session,
                    owner_id,
                    ledger_id,
                    "payment",
                    payload.transaction_date,
                    principal,
                    now,
                )
            allocated = (
                None
                if plan is None
                else persist_allocation(
                    session, owner_id, ledger_id, request, payer.id, key, plan, groups, now
                )
            )
            result = (
                BusinessPaymentResponse(
                    kind="business_payment",
                    id=payload.id,
                    ledger_id=ledger_id,
                    operation_id=principal.operation_id,
                    journal_id=principal.journal_id,
                    initial_allocation=allocated,
                )
                if payment
                else allocated
            )
            anchor = principal.operation_id if payment else groups[0].operation_id
            session.execute(
                insert(CommandReceipt).values(
                    ledger_id=ledger_id,
                    key=key,
                    hash_version=2,
                    request_hash=digest,
                    response=result.model_dump(mode="json"),
                    response_status=201,
                    operation_id=anchor,
                    created_at=now,
                )
            )
            return result
