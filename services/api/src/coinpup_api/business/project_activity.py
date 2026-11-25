"""Read period classifications and closing document control balances by project and asset."""

from datetime import date
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy import and_, case, func, literal, or_, select, union_all

from coinpup_api.business.models import BusinessProject
from coinpup_api.business.project_payment_buckets import project_payment_buckets
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import Account, AssetRecord, Journal, JournalLine
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.service import LedgerError, LedgerService, _not_found, _page

Quantity = Annotated[StrictStr, Field(max_length=40, pattern=r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")]


class ProjectAssetActivity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    asset_id: str
    income: Quantity
    cost: Quantity
    net: Quantity
    closing_receivable: Quantity
    closing_payable: Quantity
    attributed_received: Quantity
    attributed_paid: Quantity


class ProjectActivityPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ledger_id: UUID
    project_id: UUID
    start: date
    end: date
    offset: int
    limit: int
    has_more: bool
    items: tuple[ProjectAssetActivity, ...]


class ProjectActivityReads(LedgerService):
    def page(self, owner, ledger, project, *, start, end, limit=50, offset=0):
        _page(limit, offset)
        if type(start) is not date or type(end) is not date or start > end:
            raise LedgerError(
                "invalid_project_period", 422, "Choose an ordered range of business dates."
            )
        with self._transaction(owner, read_only=True) as session:
            self._ledger(session, owner, ledger)
            if (
                session.scalar(
                    select(BusinessProject.id).where(
                        BusinessProject.id == project, BusinessProject.ledger_id == ledger
                    )
                )
                is None
            ):
                raise _not_found()
            line, j, a = JournalLine, Journal, Account
            in_period = j.recognition_date >= start
            category = and_(in_period, line.role.in_(("income", "expense")))
            control = and_(
                line.role == "account",
                line.document_id.is_not(None),
                line.document_line_id.is_not(None),
                a.system_key.in_(("receivable.customer", "payable.supplier")),
            )

            def total(condition, amount):
                return func.sum(case((condition, amount), else_=0))

            buckets = (
                select(
                    line.asset_id,
                    total(and_(in_period, line.role == "income"), -line.amount).label("income"),
                    total(and_(in_period, line.role == "expense"), line.amount).label("cost"),
                    total(and_(control, a.system_key == "receivable.customer"), line.amount).label(
                        "receivable"
                    ),
                    total(and_(control, a.system_key == "payable.supplier"), -line.amount).label(
                        "payable"
                    ),
                )
                .select_from(line)
                .join(j, and_(j.ledger_id == line.ledger_id, j.id == line.journal_id))
                .outerjoin(a, and_(a.ledger_id == line.ledger_id, a.id == line.account_id))
                .where(
                    line.ledger_id == ledger,
                    line.project_id == project,
                    j.sealed.is_(True),
                    j.recognition_date <= end,
                    or_(category, control),
                )
                .group_by(line.asset_id)
                .subquery()
            )
            accrual = buckets.c
            paid = project_payment_buckets(ledger, project, start, end).subquery().c
            facts = (
                union_all(
                    select(
                        accrual.asset_id,
                        accrual.income,
                        accrual.cost,
                        accrual.receivable,
                        accrual.payable,
                        literal(0).label("received"),
                        literal(0).label("paid"),
                    ),
                    select(paid.asset_id, 0, 0, 0, 0, paid.received, paid.paid),
                )
                .subquery()
                .c
            )
            cols = ("income", "cost", "receivable", "payable", "received", "paid")
            combined = (
                select(facts.asset_id, *(func.sum(facts[k]).label(k) for k in cols))
                .group_by(facts.asset_id)
                .subquery()
            )
            b = combined.c
            rows = session.execute(
                select(AssetRecord, *(b[k] for k in cols))
                .join(combined, b.asset_id == AssetRecord.asset_id)
                .where(or_(*(b[k] != 0 for k in cols)))
                .order_by(AssetRecord.asset_id)
                .limit(limit + 1)
                .offset(offset)
            ).all()
            items = []
            try:
                for asset, *values in rows[:limit]:
                    catalog = asset_definition(asset)
                    i, c, r, p, received, paid = (Amount.from_decimal(v, catalog) for v in values)
                    items.append(
                        ProjectAssetActivity(
                            asset_id=asset.asset_id,
                            income=i.to_string(),
                            cost=c.to_string(),
                            net=(i - c).to_string(),
                            closing_receivable=r.to_string(),
                            closing_payable=p.to_string(),
                            attributed_received=received.to_string(),
                            attributed_paid=paid.to_string(),
                        )
                    )
            except MoneyError as exc:
                if exc.code == "amount_range":
                    raise LedgerError(
                        "project_activity_range", 422, "A total exceeds the exact report range."
                    ) from None
                raise LedgerError(
                    "project_activity_integrity", 503, "Stored project quantities are inconsistent."
                ) from None
            return ProjectActivityPage(
                ledger_id=ledger,
                project_id=project,
                start=start,
                end=end,
                offset=offset,
                limit=limit,
                has_more=len(rows) > limit,
                items=tuple(items),
            )
