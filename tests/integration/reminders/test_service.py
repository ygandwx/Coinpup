"""Fictional reminders retain manual decisions and enforce owner/version boundaries."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from uuid import uuid4

import pytest
from coinpup_api.ledger.schemas import EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from coinpup_api.reminders.catalog import VERSION
from coinpup_api.reminders.schemas import (
    ReminderCreate,
    ReminderEdit,
    ReminderManual,
    ReminderRecalculate,
    ReminderTransition,
    RuleSelection,
)
from coinpup_api.reminders.service import ReminderService
from coinpup_api.sync.service import ChangeService

from tests.integration.ledger.test_posting_service_database import financial_counts
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def create(s, **changes):
    return ReminderService(s["engine"]).create_event(
        s["owner"],
        s["ledger"],
        ReminderCreate.model_validate(
            dict(
                id=uuid4(),
                event_kind="certificate",
                title="Fictional certificate",
                rule=dict(
                    rule_id="certificate.expiry",
                    rule_version=VERSION,
                    expiry_date="2027-01-31",
                    applicability_confirmed=True,
                ),
            )
            | changes
        ),
    )


def test_manual_recalculation_completion_history_and_restore(ledger_setup):
    s = ledger_setup
    service = ReminderService(s["engine"])
    args = s["owner"], s["ledger"]
    before = financial_counts(s["engine"])
    rules = service.list_rules(*args)
    assert len(rules) == 6 and all(rule.version == VERSION for rule in rules)
    with pytest.raises(LedgerError) as failure:
        service.list_rules(uuid4(), s["ledger"])
    assert failure.value.code == "not_found"
    row = create(s)
    assert row.effective_date == date(2027, 1, 31)
    row = service.set_manual_date(
        *args,
        row.id,
        ReminderManual(
            expected_version=1, manual_due_date="2027-02-10", reason="Fictional verified notice"
        ),
    )
    row = service.transition_event(
        *args, row.id, ReminderTransition(expected_version=2, action="complete")
    )
    row = service.recalculate_event(
        *args,
        row.id,
        ReminderRecalculate(
            expected_version=3,
            rule=RuleSelection(
                rule_id="certificate.expiry",
                rule_version=VERSION,
                expiry_date="2027-02-15",
                applicability_confirmed=True,
            ),
        ),
    )
    assert row.completed and row.calculated_date == date(2027, 2, 15)
    assert row.effective_date == date(2027, 2, 10)
    row = service.transition_event(
        *args, row.id, ReminderTransition(expected_version=4, action="archive")
    )
    assert service.list_events(*args, include_archived=False) == []
    row = service.transition_event(
        *args, row.id, ReminderTransition(expected_version=5, action="restore")
    )
    assert row.completed
    assert service.list_events(*args, include_completed=False) == []
    row = service.set_manual_date(
        *args,
        row.id,
        ReminderManual(
            expected_version=6, manual_due_date=None, reason="Fictional notice withdrawn"
        ),
    )
    assert row.effective_date == date(2027, 2, 15)
    history = service.list_revisions(*args, row.id)
    assert [x.version for x in history] == list(range(1, 8))
    assert history[1].snapshot["manual_due_date"] == "2027-02-10"
    assert history[-1].snapshot["last_reason"] == "Fictional notice withdrawn"
    assert [x.version for x in service.list_revisions(*args, row.id, limit=2, offset=2)] == [3, 4]
    logs = ChangeService(s["engine"]).list_changes(s["owner"], limit=200)
    assert len([x for x in logs.changes if x.entity_type.startswith("reminder_")]) == 14
    assert financial_counts(s["engine"]) == before


def test_company_parameters_are_captured_then_explicitly_recalculated(ledger_setup):
    s = ledger_setup
    entity = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="company",
            name="Fictional China LLC",
            country_code="CN",
            company_type="limited_liability",
            registration_date="2020-03-01",
            base_asset_id="USD",
            template_key="business_default",
        ),
    )
    service = ReminderService(s["engine"])
    args = s["owner"], entity.ledger.id
    selection = RuleSelection(
        rule_id="cn.company.annual_report",
        rule_version=VERSION,
        filing_year=2027,
        applicability_confirmed=True,
    )
    row = service.create_event(
        *args,
        ReminderCreate(
            id=uuid4(), event_kind="annual", title="Fictional annual report", rule=selection
        ),
    )
    assert row.effective_date == date(2027, 6, 30)
    s["structure"].update_entity(
        s["owner"],
        entity.id,
        EntityUpdate(expected_version=entity.version, registration_date="2027-03-01"),
    )
    assert (
        service.get_event(*args, row.id).evaluation["inputs"]["registration_date"] == "2020-03-01"
    )
    row = service.recalculate_event(
        *args, row.id, ReminderRecalculate(expected_version=1, rule=selection)
    )
    assert row.evaluation_status == "not_applicable" and row.effective_date is None
    assert service.list_revisions(*args, row.id)[0].snapshot["calculated_date"] == "2027-06-30"


@pytest.mark.parametrize("method", ["get_event", "list_revisions", "edit_event"])
def test_foreign_owner_and_wrong_ledger_are_invisible(ledger_setup, method):
    s = ledger_setup
    row = create(s)
    service = ReminderService(s["engine"])
    tail = (
        [ReminderEdit(expected_version=1, title="Fictional denied")]
        if method == "edit_event"
        else []
    )
    other = s["structure"].create_entity(
        s["owner"], EntityCreate(kind="personal", name="Fictional other", base_asset_id="USD")
    )
    for owner, ledger in [(uuid4(), s["ledger"]), (s["owner"], other.ledger.id)]:
        with pytest.raises(LedgerError) as failure:
            getattr(service, method)(owner, ledger, row.id, *tail)
        assert failure.value.code == "not_found"
    assert service.get_event(s["owner"], s["ledger"], row.id).version == 1


def test_duplicate_identity_stale_write_archive_and_invalid_rule_leave_no_history(ledger_setup):
    s = ledger_setup
    row = create(s)
    service = ReminderService(s["engine"])
    args = s["owner"], s["ledger"], row.id
    with pytest.raises(LedgerError, match="already exists"):
        create(s, id=row.id)
    for rule in [
        RuleSelection(rule_id="certificate.expiry", rule_version="unknown"),
        RuleSelection(rule_id="cn.company.annual_report", rule_version=VERSION),
    ]:
        with pytest.raises(LedgerError) as failure:
            service.recalculate_event(*args, ReminderRecalculate(expected_version=1, rule=rule))
        assert failure.value.status == 422
    service.edit_event(
        *args, ReminderEdit(expected_version=1, title="Fictional edited", notes="Fictional note")
    )
    with pytest.raises(LedgerError) as failure:
        service.edit_event(
            *args,
            ReminderEdit(expected_version=1, title="Fictional edited", notes="Fictional note"),
        )
    assert failure.value.code == "version_conflict"
    service.transition_event(*args, ReminderTransition(expected_version=2, action="archive"))
    with pytest.raises(LedgerError) as failure:
        service.edit_event(*args, ReminderEdit(expected_version=3, title="Fictional denied"))
    assert failure.value.code == "reminder_archived"
    assert len(service.list_revisions(*args)) == 3


def test_concurrent_same_version_has_one_winner(ledger_setup):
    s = ledger_setup
    row = create(s)
    service = ReminderService(s["engine"])
    args = s["owner"], s["ledger"], row.id

    def attempt(index):
        try:
            return service.edit_event(
                *args, ReminderEdit(expected_version=1, title=f"Fictional update {index}")
            ).version
        except LedgerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, [1, 2]))
    assert set(outcomes) == {2, "version_conflict"}
    assert len(service.list_revisions(*args)) == 2


def test_manual_tax_and_missing_rule_inputs_do_not_invent_dates(ledger_setup):
    s = ledger_setup
    manual = create(
        s,
        event_kind="tax",
        rule=None,
        manual_due_date="2027-01-31",
        manual_reason="Fictional tax notice",
    )
    assert manual.effective_date == date(2027, 1, 31) and manual.calculated_date is None
    missing = create(s, rule=dict(rule_id="certificate.expiry", rule_version=VERSION))
    assert missing.evaluation_status == "missing_parameters" and missing.effective_date is None


def test_archived_entity_allows_history_but_no_reminder_write(ledger_setup):
    s = ledger_setup
    row = create(s)
    service = ReminderService(s["engine"])
    s["structure"].update_entity(
        s["owner"],
        s["entity"].id,
        EntityUpdate(expected_version=s["entity"].version, archived=True),
    )
    args = s["owner"], s["ledger"], row.id
    assert service.get_event(*args).id == row.id
    assert len(service.list_revisions(*args)) == 1
    with pytest.raises(LedgerError) as failure:
        service.transition_event(*args, ReminderTransition(expected_version=1, action="complete"))
    assert failure.value.code == "entity_archived"
    assert service.get_event(*args).version == 1


@pytest.mark.parametrize("action", ["reopen", "restore", "clear_manual"])
def test_invalid_state_transitions_roll_back_without_revision(ledger_setup, action):
    s = ledger_setup
    row = create(s)
    service = ReminderService(s["engine"])
    args = s["owner"], s["ledger"], row.id
    with pytest.raises(LedgerError) as failure:
        if action == "clear_manual":
            service.set_manual_date(
                *args,
                ReminderManual(
                    expected_version=1,
                    manual_due_date=None,
                    reason="Fictional withdrawal without override",
                ),
            )
        else:
            service.transition_event(*args, ReminderTransition(expected_version=1, action=action))
    assert failure.value.code == "constraint_conflict"
    assert service.get_event(*args).version == 1
    assert len(service.list_revisions(*args)) == 1
