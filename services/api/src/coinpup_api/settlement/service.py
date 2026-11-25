"""Binary, single-transaction posting for opt-in interledger advances."""

from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from coinpup_api.business.settlement_rows import PostingGroup, persist_posting
from coinpup_api.ledger.balances import BalanceQueries
from coinpup_api.ledger.common import PostingCore, _invalid_money, asset_definition
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.idempotency import _IDEMPOTENCY_KEY, _v2_hash
from coinpup_api.ledger.models import AssetRecord, Category, Entity, Ledger
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.period_checks import require_open_period
from coinpup_api.ledger.posting_storage import PostingLine, prepare_journal_lines
from coinpup_api.ledger.service import LedgerError, _not_found, _page
from coinpup_api.settlement.models import InterledgerGroup, InterledgerPreference
from coinpup_api.settlement.schemas import (
    InterledgerAdvance,
    InterledgerDetail,
    InterledgerRepayment,
    InterledgerSettings,
)

RECEIVABLE = "intercompany.receivable"
PAYABLE = "intercompany.payable"


def _quantity(text, asset):
    quantity = Amount.parse(text, asset)
    if quantity.minor_units <= 0:
        raise MoneyError("amount_positive", "The quantity must be positive.")
    return quantity


def _receipt(header, response_type):
    try:
        return response_type.model_validate(header.response)
    except ValidationError:
        raise LedgerError(
            "ledger_integrity", 503, "Stored interledger receipt is invalid."
        ) from None


def _remaining(root, asset, repaid):
    try:
        total = Amount.from_decimal(root.amount, asset)
        used = Amount.from_decimal(repaid, asset) if repaid is not None else Amount(asset, 0)
        if used.minor_units < 0 or used > total:
            raise MoneyError("invalid_capacity", "Repayment exceeds the original principal.")
        return total - used
    except MoneyError:
        raise LedgerError(
            "ledger_integrity", 503, "Stored interledger amount is invalid."
        ) from None


def _replay(session, owner, key, digest, response_type):
    existing = session.scalar(
        select(InterledgerGroup).where(
            InterledgerGroup.owner_id == owner, InterledgerGroup.command_key == key
        )
    )
    if existing is None:
        return None
    if existing.request_hash != digest:
        raise LedgerError(
            "idempotency_conflict", 409, "This command key was used for another request."
        )
    return _receipt(existing, response_type)


def _key(key):
    if not isinstance(key, str) or not _IDEMPOTENCY_KEY.fullmatch(key):
        raise LedgerError("invalid_idempotency_key", 422, "Use 1–128 visible ASCII characters.")


def _source(session, owner, advance):
    header = session.get(InterledgerGroup, advance)
    if header is None or header.owner_id != owner or header.kind != "advance":
        raise _not_found()
    return header


def _journal(ledger, principal, control, counterparty, owner, description, catalog):
    journal = uuid4()
    lines = (
        PostingLine(
            journal_id=journal,
            ledger_id=ledger,
            line_no=1,
            asset_id=principal["amount"].asset.asset_id,
            **principal,
        ),
        PostingLine(
            journal_id=journal,
            ledger_id=ledger,
            line_no=2,
            role="account",
            asset_id=principal["amount"].asset.asset_id,
            account_id=control,
            amount=-principal["amount"],
            counterparty_entity_id=counterparty,
            dimension_owner_id=owner,
        ),
    )
    return PostingGroup(
        uuid4(), journal, description, lines, tuple(prepare_journal_lines(lines, catalog))
    )


class InterledgerService(BalanceQueries, PostingCore):
    def settings(self, owner):
        with self._transaction(owner, read_only=True) as session:
            settings = session.get(InterledgerPreference, owner)
            return InterledgerSettings(enabled=settings.enabled if settings else False)

    def set_enabled(self, owner, payload):
        with self._transaction(owner, write=True, read_committed=True) as session:
            settings = session.get(InterledgerPreference, owner)
            if settings is None and payload.enabled:
                session.add(InterledgerPreference(owner_id=owner, enabled=True, version=1))
            elif settings is not None and settings.enabled != payload.enabled:
                settings.enabled = payload.enabled
                settings.version += 1
        return payload

    @staticmethod
    def _locked_pair(session, owner, payer, beneficiary, payload):
        settings = session.get(InterledgerPreference, owner)
        if settings is None or not settings.enabled:
            raise LedgerError(
                "interledger_disabled", 409, "Enable interledger advances before posting."
            )
        if payer == beneficiary:
            raise LedgerError("invalid_interledger_pair", 422, "Select two different ledgers.")
        ledgers = session.scalars(
            select(Ledger)
            .where(Ledger.owner_id == owner, Ledger.id.in_([payer, beneficiary]))
            .order_by(Ledger.id)
            .with_for_update()
        ).all()
        if len(ledgers) != 2:
            raise _not_found()
        parties = {ledger.id: ledger.entity_id for ledger in ledgers}
        entities = session.scalars(
            select(Entity)
            .where(Entity.owner_id == owner, Entity.id.in_(parties.values()))
            .order_by(Entity.id)
            .with_for_update()
        ).all()
        if len(entities) != 2:
            raise _not_found()
        if any(entity.archived for entity in entities):
            raise LedgerError("entity_archived", 409, "Restore both entities before posting.")
        for ledger in ledgers:
            require_open_period(session, ledger.id, payload)
        return parties

    def _record_pair(
        self,
        session,
        owner,
        payer,
        beneficiary,
        counterparties,
        payload,
        quantity,
        payer_account,
        *,
        expense_category=None,
        beneficiary_account=None,
    ):
        asset = quantity.asset.asset_id
        catalog = {asset: quantity.asset}
        controls = ControlAccounts(self.engine)
        receivable = controls.ensure(session, owner, payer, {RECEIVABLE: {asset}})[RECEIVABLE].id
        payable = controls.ensure(session, owner, beneficiary, {PAYABLE: {asset}})[PAYABLE].id
        is_advance = beneficiary_account is None
        bank = {
            "role": "account",
            "account_id": payer_account,
            "amount": -quantity if is_advance else quantity,
        }
        expense_or_bank = (
            {"role": "expense", "category_id": expense_category, "amount": quantity}
            if is_advance
            else {"role": "account", "account_id": beneficiary_account, "amount": -quantity}
        )
        first = _journal(
            payer,
            bank,
            receivable,
            counterparties[beneficiary],
            owner,
            payload.description,
            catalog,
        )
        second = _journal(
            beneficiary,
            expense_or_bank,
            payable,
            counterparties[payer],
            owner,
            payload.description,
            catalog,
        )
        self._validate_balance_deltas(session, payer, first.lines, catalog)
        self._validate_balance_deltas(session, beneficiary, second.lines, catalog)
        kind = "advance" if is_advance else "repayment"
        now = session.scalar(select(func.clock_timestamp()))
        persist_posting(session, owner, payer, kind, payload.transaction_date, first, now)
        persist_posting(session, owner, beneficiary, kind, payload.transaction_date, second, now)
        return first.operation_id, second.operation_id

    def advance(self, owner, payload, key, *, raw_body=None):
        _key(key)
        payer, beneficiary = payload.payer_ledger_id, payload.beneficiary_ledger_id
        digest = _v2_hash("interledger_advance", payer, payload, raw_body)
        try:
            with self._transaction(owner, write=True, read_committed=True) as session:
                existing = _replay(session, owner, key, digest, InterledgerAdvance)
                if existing is not None:
                    return existing
                counterparts = self._locked_pair(session, owner, payer, beneficiary, payload)
                asset = asset_definition(self._assets(session, [payload.asset_id])[0])
                quantity = _quantity(payload.amount, asset)
                self._account(session, payer, payload.payer_account_id, asset.asset_id)
                category = session.scalar(
                    select(Category).where(
                        Category.id == payload.expense_category_id,
                        Category.ledger_id == beneficiary,
                    )
                )
                if category is None:
                    raise _not_found()
                if category.kind != "expense":
                    raise LedgerError("category_kind_mismatch", 422, "Select an expense category.")
                if category.archived:
                    raise LedgerError(
                        "category_archived", 409, "Restore the category before posting."
                    )
                first, second = self._record_pair(
                    session,
                    owner,
                    payer,
                    beneficiary,
                    counterparts,
                    payload,
                    quantity,
                    payload.payer_account_id,
                    expense_category=category.id,
                )
                response = InterledgerAdvance(
                    id=uuid4(),
                    payer_ledger_id=payer,
                    beneficiary_ledger_id=beneficiary,
                    payer_account_id=payload.payer_account_id,
                    expense_category_id=category.id,
                    asset_id=asset.asset_id,
                    amount=quantity.to_string(),
                    outstanding_amount=quantity.to_string(),
                    transaction_date=payload.transaction_date,
                    description=payload.description,
                    payer_operation_id=first,
                    beneficiary_operation_id=second,
                )
                session.add(
                    InterledgerGroup(
                        **response.model_dump(exclude={"outstanding_amount", "amount"}),
                        owner_id=owner,
                        kind="advance",
                        expense_kind="expense",
                        amount=quantity.to_decimal(),
                        command_key=key,
                        request_hash=digest,
                        response=response.model_dump(mode="json"),
                    )
                )
                return response
        except MoneyError as error:
            raise _invalid_money(error) from None

    def repay(self, owner, advance, payload, key, *, raw_body=None):
        _key(key)
        try:
            with self._transaction(owner, write=True, read_committed=True) as session:
                root = _source(session, owner, advance)
                payer, beneficiary = root.payer_ledger_id, root.beneficiary_ledger_id
                digest = _v2_hash(
                    "interledger_repayment", payer, payload, raw_body, operation_id=advance
                )
                existing = _replay(session, owner, key, digest, InterledgerRepayment)
                if existing is not None:
                    return existing
                counterparts = self._locked_pair(session, owner, payer, beneficiary, payload)
                asset = asset_definition(self._assets(session, [root.asset_id])[0])
                quantity = _quantity(payload.amount, asset)
                if payload.transaction_date < root.transaction_date:
                    raise LedgerError(
                        "repayment_before_advance", 422, "Repayment precedes the advance."
                    )
                repaid = session.scalar(
                    select(func.sum(InterledgerGroup.amount)).where(
                        InterledgerGroup.advance_id == advance
                    )
                )
                if quantity > _remaining(root, asset, repaid):
                    raise LedgerError(
                        "over_repayment", 409, "Repayment exceeds the outstanding principal."
                    )
                self._account(session, payer, payload.payer_receipt_account_id, asset.asset_id)
                self._account(
                    session, beneficiary, payload.beneficiary_payment_account_id, asset.asset_id
                )
                first, second = self._record_pair(
                    session,
                    owner,
                    payer,
                    beneficiary,
                    counterparts,
                    payload,
                    quantity,
                    payload.payer_receipt_account_id,
                    beneficiary_account=payload.beneficiary_payment_account_id,
                )
                response = InterledgerRepayment(
                    id=uuid4(),
                    advance_id=advance,
                    amount=quantity.to_string(),
                    beneficiary_payment_account_id=payload.beneficiary_payment_account_id,
                    payer_receipt_account_id=payload.payer_receipt_account_id,
                    transaction_date=payload.transaction_date,
                    description=payload.description,
                    payer_operation_id=first,
                    beneficiary_operation_id=second,
                )
                session.add(
                    InterledgerGroup(
                        id=response.id,
                        owner_id=owner,
                        kind="repayment",
                        advance_id=advance,
                        advance_kind="advance",
                        payer_ledger_id=payer,
                        beneficiary_ledger_id=beneficiary,
                        asset_id=asset.asset_id,
                        payer_account_id=response.payer_receipt_account_id,
                        beneficiary_account_id=response.beneficiary_payment_account_id,
                        amount=quantity.to_decimal(),
                        transaction_date=payload.transaction_date,
                        description=payload.description,
                        payer_operation_id=first,
                        beneficiary_operation_id=second,
                        command_key=key,
                        request_hash=digest,
                        response=response.model_dump(mode="json"),
                    )
                )
                return response
        except MoneyError as error:
            raise _invalid_money(error) from None

    @staticmethod
    def _advance_read(row, asset, repaid):
        receipt = _receipt(row, InterledgerAdvance)
        return receipt.model_copy(
            update={
                "outstanding_amount": _remaining(row, asset_definition(asset), repaid).to_string()
            }
        )

    def list_advances(self, owner, limit=100, offset=0):
        _page(limit, offset)
        children = aliased(InterledgerGroup)
        repaid = select(func.sum(children.amount)).where(children.advance_id == InterledgerGroup.id)
        with self._transaction(owner, read_only=True) as session:
            rows = session.execute(
                select(InterledgerGroup, AssetRecord, repaid.scalar_subquery())
                .join(AssetRecord, AssetRecord.asset_id == InterledgerGroup.asset_id)
                .where(InterledgerGroup.owner_id == owner, InterledgerGroup.kind == "advance")
                .order_by(InterledgerGroup.transaction_date.desc(), InterledgerGroup.id.desc())
                .limit(limit)
                .offset(offset)
            )
            return [self._advance_read(root, asset, total) for root, asset, total in rows]

    def detail(self, owner, advance):
        with self._transaction(owner, read_only=True) as session:
            root = _source(session, owner, advance)
            asset = session.get(AssetRecord, root.asset_id)
            repayments = session.scalars(
                select(InterledgerGroup)
                .where(InterledgerGroup.advance_id == advance)
                .order_by(InterledgerGroup.transaction_date, InterledgerGroup.id)
            ).all()
            repaid = session.scalar(
                select(func.sum(InterledgerGroup.amount)).where(
                    InterledgerGroup.advance_id == advance
                )
            )
            view = self._advance_read(root, asset, repaid)
            return InterledgerDetail(
                **view.model_dump(),
                repayments=[_receipt(item, InterledgerRepayment) for item in repayments],
            )
