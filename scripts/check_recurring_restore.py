"""Fictional recurring rows for the guarded disposable bundle checker, not a job API."""

from datetime import date
from uuid import uuid4

from coinpup_api.business.draft_schemas import BusinessDraftCreate, BusinessDraftLineInput
from coinpup_api.business.drafts import DraftService
from coinpup_api.business.models import (
    BusinessDocument,
    BusinessDocumentLine,
    RecurringInvoiceInstance,
    RecurringInvoiceRule,
)
from coinpup_api.business.recurrence_calendar import instantiate_draft
from coinpup_api.sync.locking import acquire_write_lock
from sqlalchemy import select
from sqlalchemy.orm import Session


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
    generated = instantiate_draft(template, rule_id, 0, date(2026, 11, 9))
    service = DraftService(engine)
    with Session(engine) as session, session.begin():
        acquire_write_lock(session)
        _, entity = service._ledger(session, owner, ledger, write=True)
        header, lines, _ = service._prepare(session, ledger, entity, generated)
        rule = RecurringInvoiceRule(
            id=rule_id,
            ledger_id=ledger,
            name="Fictional recurring recovery",
            timezone_name="Asia/Shanghai",
            anchor_date=generated.issue_date,
            frequency="month",
            interval_count=1,
            source_document_id=source.id,
            source_version=source.version,
            template_input=template.model_dump(mode="json"),
        )
        session.add(rule)
        session.add(BusinessDocument(id=generated.id, **header))
        session.flush()
        session.add_all(BusinessDocumentLine(document_id=generated.id, **line) for line in lines)
        session.flush()
        session.add(
            RecurringInvoiceInstance(
                id=generated.id,
                ledger_id=ledger,
                rule_id=rule.id,
                occurrence_index=0,
                scheduled_date=generated.issue_date,
                rule_version=1,
                original_input=generated.model_dump(mode="json"),
            )
        )
        session.flush()
        rule.version += 1
        rule.next_index += 1
    return dict(
        rule=rule_id,
        ledger=ledger,
        source_version=source.version,
        template=template.model_dump(mode="json"),
        generated=generated.model_dump(mode="json"),
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
