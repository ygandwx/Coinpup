"""Fictional recurring service generation and replay across complete bundle recovery."""

from datetime import UTC, date, datetime
from uuid import uuid4

from coinpup_api.business.draft_schemas import BusinessDraftCreate, BusinessDraftLineInput
from coinpup_api.business.models import RecurringInvoiceInstance, RecurringInvoiceRule
from coinpup_api.business.recurring_generation import RecurringGenerationService
from coinpup_api.business.recurring_rules import RecurringRuleService
from coinpup_api.business.recurring_schemas import RecurringRuleCreate
from sqlalchemy import select


def seed_recurring(engine, owner, ledger, source):
    values = source.model_dump(
        include=set(BusinessDraftCreate.model_fields) - {"lines"}, mode="json"
    )
    values["lines"] = [
        line.model_dump(include=set(BusinessDraftLineInput.model_fields), mode="json")
        for line in source.lines
    ]
    template = BusinessDraftCreate.model_validate(values)
    rule_id = uuid4()
    RecurringRuleService(engine).create_rule(
        owner,
        ledger,
        RecurringRuleCreate(
            id=rule_id,
            name="Fictional 恢复周期",
            timezone_name="Asia/Shanghai",
            anchor_date=date(2026, 11, 9),
            frequency="month",
            interval_count=1,
            source_document_id=source.id,
            source_version=source.version,
        ),
    )
    receipt = RecurringGenerationService(engine).generate_occurrence(
        owner, ledger, rule_id, 0, now=datetime(2030, 1, 1, tzinfo=UTC)
    )
    return dict(
        rule=rule_id,
        owner=owner,
        ledger=ledger,
        source_version=source.version,
        template=template.model_dump(mode="json"),
        generated=receipt.original_input.model_dump(mode="json"),
        receipt=receipt,
    )


def verify_recurring(engine, evidence):
    with engine.connect() as c:
        rule = (
            c.execute(
                select(RecurringInvoiceRule.__table__).where(
                    RecurringInvoiceRule.id == evidence["rule"]
                )
            )
            .mappings()
            .one()
        )
        instance = (
            c.execute(
                select(RecurringInvoiceInstance.__table__).where(
                    RecurringInvoiceInstance.rule_id == evidence["rule"]
                )
            )
            .mappings()
            .one()
        )
        assert rule["ledger_id"] == instance["ledger_id"] == evidence["ledger"]
        assert rule["template_input"] == evidence["template"]
        assert rule["source_version"] == evidence["source_version"]
        assert rule["next_index"] == 1 and rule["version"] == 2 and not rule["archived"]
        assert instance["original_input"] == evidence["generated"]
        assert str(instance["id"]) == evidence["generated"]["id"]
        assert instance["occurrence_index"] == 0 and instance["rule_version"] == 1
        assert instance["version"] == 1 and not instance["archived"]

    replay = RecurringGenerationService(engine).generate_occurrence(
        evidence["owner"],
        evidence["ledger"],
        evidence["rule"],
        0,
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert replay == evidence["receipt"]
