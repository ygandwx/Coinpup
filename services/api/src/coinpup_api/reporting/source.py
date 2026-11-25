"""Historical sources read in the report owner's existing database snapshot."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, func, or_, select

from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.models import (
    Account,
    AccountAsset,
    AssetRecord,
    FinancialOperation,
    Journal,
    JournalLine,
)
from coinpup_api.ledger.money import Amount
from coinpup_api.reporting.statement import CashRepaymentLink, SourceRow
from coinpup_api.settlement.models import InterledgerGroup


@dataclass(frozen=True, slots=True)
class OriginalFact:
    row: SourceRow
    date: date | None
    operation_id: UUID | None
    operation_kind: str | None
    account_id: UUID | None
    category_id: UUID | None
    role: str
    description: str


def _range(statement, column, start, end):
    if start is not None:
        statement = statement.where(column >= start)
    if end is not None:
        statement = statement.where(column <= end)
    return statement


def journal_facts(session, scope, view, start, end):
    line, journal, operation = JournalLine, Journal, FinancialOperation
    statement = (
        select(line, journal, operation.kind, AssetRecord)
        .select_from(line)
        .join(journal, and_(journal.id == line.journal_id, journal.ledger_id == line.ledger_id))
        .join(
            operation,
            and_(operation.id == journal.operation_id, operation.ledger_id == journal.ledger_id),
        )
        .join(AssetRecord, AssetRecord.asset_id == line.asset_id)
        .where(line.ledger_id.in_(scope), journal.sealed.is_(True))
    )
    cash = view == "cash"
    if cash:
        statement = statement.join(
            Account, and_(Account.id == line.account_id, Account.ledger_id == line.ledger_id)
        ).where(
            line.role == "account", Account.account_class == "money", operation.kind != "opening"
        )
        day = journal.transaction_date
    else:
        statement = statement.where(line.role.in_(("income", "expense")))
        day = journal.recognition_date
    statement = _range(statement, day, start, end).order_by(
        day, line.ledger_id, journal.id, line.line_no
    )

    facts = []
    for item, header, kind, record in session.execute(statement):
        original = Amount.from_decimal(item.amount, asset_definition(record))
        facts.append(
            OriginalFact(
                SourceRow(item.ledger_id, item.id, original if cash else -original),
                header.transaction_date if cash else header.recognition_date,
                header.operation_id,
                kind,
                item.account_id,
                item.category_id,
                item.role,
                header.description,
            )
        )
    return tuple(facts)


def balance_facts(session, scope, end):
    line, journal, link = JournalLine, Journal, AccountAsset
    posted = (
        _range(
            select(
                line.ledger_id,
                line.account_id,
                line.asset_id,
                func.sum(line.amount).label("amount"),
            )
            .select_from(line)
            .join(journal, and_(journal.id == line.journal_id, journal.ledger_id == line.ledger_id))
            .where(line.ledger_id.in_(scope), line.role == "account", journal.sealed.is_(True)),
            journal.transaction_date,
            None,
            end,
        )
        .group_by(line.ledger_id, line.account_id, line.asset_id)
        .subquery()
    )
    statement = (
        select(Account, AssetRecord, posted.c.amount)
        .select_from(Account)
        .join(link, and_(link.account_id == Account.id, link.ledger_id == Account.ledger_id))
        .join(AssetRecord, AssetRecord.asset_id == link.asset_id)
        .outerjoin(
            posted,
            and_(
                posted.c.ledger_id == link.ledger_id,
                posted.c.account_id == link.account_id,
                posted.c.asset_id == link.asset_id,
            ),
        )
        .where(Account.ledger_id.in_(scope), Account.account_class == "money")
        .order_by(Account.ledger_id, Account.id, link.asset_id)
    )
    return tuple(
        OriginalFact(
            SourceRow(
                account.ledger_id,
                account.id,
                Amount.from_decimal(
                    amount if amount is not None else Decimal(0), asset_definition(record)
                ),
            ),
            end,
            None,
            None,
            account.id,
            None,
            "account",
            account.name,
        )
        for account, record, amount in session.execute(statement)
    )


def linked_repayments(session, owner, scope, facts):
    operations = defaultdict(list)
    for fact in facts:
        if fact.operation_kind == "repayment":
            operations[fact.operation_id].append(fact)
    if not operations:
        return (), {}

    groups = session.scalars(
        select(InterledgerGroup).where(
            InterledgerGroup.owner_id == owner,
            InterledgerGroup.kind == "repayment",
            or_(
                InterledgerGroup.payer_operation_id.in_(operations),
                InterledgerGroup.beneficiary_operation_id.in_(operations),
            ),
        )
    )
    links, memberships = [], {}

    def match(group, ledger, operation, account, sign):
        if ledger not in scope:
            return None
        rows = operations.get(operation, ())
        if len(rows) != 1:
            raise ValueError("Unexpected number of cash rows for a repayment.")
        fact = rows[0]
        row = fact.row
        quantity = Amount.from_decimal(group.amount, row.original.asset)
        if (
            row.ledger_id != ledger
            or fact.account_id != account
            or fact.date != group.transaction_date
            or row.original.asset.asset_id != group.asset_id
            or row.original.minor_units != sign * quantity.minor_units
            or row.reference in memberships
        ):
            raise ValueError("Repayment master and cash rows disagree.")
        memberships[row.reference] = group.id
        return row.reference

    for group in groups:
        payer = match(
            group, group.payer_ledger_id, group.payer_operation_id, group.payer_account_id, 1
        )
        beneficiary = match(
            group,
            group.beneficiary_ledger_id,
            group.beneficiary_operation_id,
            group.beneficiary_account_id,
            -1,
        )
        if payer is not None and beneficiary is not None:
            links.append(CashRepaymentLink(group.id, beneficiary, payer))
    if set(memberships) != {fact.row.reference for rows in operations.values() for fact in rows}:
        raise ValueError("Cash repayment group is missing.")
    return tuple(links), memberships
