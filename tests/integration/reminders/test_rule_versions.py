"""Fictional shipped rule versions cannot silently replace captured event evidence."""

from copy import deepcopy
from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest
from coinpup_api.reminders import service as reminder_module
from coinpup_api.reminders.catalog import VERSION, get_rule
from coinpup_api.reminders.evaluation import ReminderEvaluationError
from coinpup_api.reminders.schemas import ReminderCreate, ReminderManual, ReminderRecalculate
from coinpup_api.reminders.service import ReminderService
from coinpup_api.sync.service import ChangeService

from tests.integration.ledger.test_posting_service_database import financial_counts
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def test_fictional_rule_upgrade_requires_explicit_recalculation_and_preserves_override(
    ledger_setup, monkeypatch
):
    s = ledger_setup
    service = ReminderService(s["engine"])
    args = s["owner"], s["ledger"]
    baseline = financial_counts(s["engine"])
    template = get_rule("certificate.expiry", VERSION)
    v1 = replace(
        template, version="Fictional-R-v1", sources=("https://example.invalid/Fictional-v1",)
    )
    v2 = replace(
        v1,
        version="Fictional-R-v2",
        checked_on=date(2026, 10, 11),
        sources=("https://example.invalid/Fictional-v2",),
    )
    available = {v1.version: v1}

    def selected(identifier, version):
        if identifier != v1.id or version not in available:
            raise ReminderEvaluationError("reminder_rule_version_unknown")
        return available[version]

    monkeypatch.setattr(reminder_module, "get_rule", selected)
    monkeypatch.setattr(reminder_module, "RULES", (v1,))
    row = service.create_event(
        *args,
        ReminderCreate(
            id=uuid4(),
            event_kind="certificate",
            title="Fictional versioned certificate",
            rule=dict(
                rule_id=v1.id,
                rule_version=v1.version,
                expiry_date="2027-01-31",
                applicability_confirmed=True,
            ),
        ),
    )
    initial_evidence = deepcopy(row.evaluation)
    row = service.set_manual_date(
        *args,
        row.id,
        ReminderManual(
            expected_version=1,
            manual_due_date="2027-02-10",
            reason="Fictional user reviewed notice",
        ),
    )
    overridden = row.model_dump(mode="json")
    available[v2.version] = v2
    monkeypatch.setattr(reminder_module, "RULES", (v1, v2))
    assert [rule.version for rule in service.list_rules(*args)] == [v1.version, v2.version]
    assert service.get_event(*args, row.id).model_dump(mode="json") == overridden
    assert service.list_events(*args)[0].model_dump(mode="json") == overridden
    assert len(service.list_revisions(*args, row.id)) == 2
    row = service.recalculate_event(
        *args,
        row.id,
        ReminderRecalculate(
            expected_version=2,
            rule=dict(
                rule_id=v2.id,
                rule_version=v2.version,
                expiry_date="2027-02-15",
                applicability_confirmed=True,
            ),
        ),
    )
    assert row.version == 3 and row.calculated_date == date(2027, 2, 15)
    assert row.effective_date == date(2027, 2, 10)
    assert row.manual_reason == "Fictional user reviewed notice"
    history = service.list_revisions(*args, row.id)
    assert history[0].snapshot["evaluation"] == initial_evidence
    assert history[1].snapshot["evaluation"] == initial_evidence
    assert history[2].snapshot["evaluation"]["rule"]["version"] == v2.version
    assert history[2].snapshot["evaluation"]["rule"]["sources"] == list(v2.sources)
    logs = ChangeService(s["engine"]).list_changes(s["owner"], limit=200)
    assert (
        len([change for change in logs.changes if change.entity_type.startswith("reminder_")]) == 6
    )
    assert financial_counts(s["engine"]) == baseline
