"""Bounded catch-up per captured rule; each occurrence commits independently."""

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from coinpup_api.business.models import RecurringInvoiceRule as Rule
from coinpup_api.business.recurring_generation import RecurringGenerationService, aware_instant
from coinpup_api.ledger.models import Entity, Ledger
from coinpup_api.ledger.service import LedgerError


def run_recurring(engine, *, now, per_rule=10):
    aware_instant(now)
    if type(per_rule) is not int or not 1 <= per_rule <= 100:
        raise ValueError("The per-rule limit must be an integer from 1 to 100")
    service = RecurringGenerationService(engine)
    # Capture a finite work list before generating. Do not hold its read transaction
    # across writes, and leave newly created rules for the next invocation.
    with engine.connect() as connection:
        rules = connection.execute(
            select(Rule.id, Rule.ledger_id, Ledger.owner_id, Rule.next_index)
            .join(Ledger, Ledger.id == Rule.ledger_id)
            .join(Entity, Entity.id == Ledger.entity_id)
            .where(Rule.archived.is_(False), Entity.archived.is_(False))
            .order_by(Rule.id)
        ).all()
    results = []
    for rule in rules:
        result = dict(rule_id=str(rule.id), confirmed=0, status="limit")
        for index in range(rule.next_index, rule.next_index + per_rule):
            try:
                service.generate_occurrence(rule.owner_id, rule.ledger_id, rule.id, index, now=now)
                # A concurrent worker may have already committed this occurrence.
                # Confirmed counts receipts, not an unprovable attribution of creation.
                result["confirmed"] += 1
            except LedgerError as error:
                result["status"] = {
                    "recurrence_not_due": "not_due",
                    "recurrence_archived": "paused",
                    "entity_archived": "paused",
                }.get(error.code, "failed")
                result["code"] = error.code
                break
            except SQLAlchemyError:
                result.update(status="failed", code="recurrence_database")
                break
        results.append(result)
    return dict(
        rules=results,
        confirmed=sum(row["confirmed"] for row in results),
        failures=sum(row["status"] == "failed" for row in results),
    )
