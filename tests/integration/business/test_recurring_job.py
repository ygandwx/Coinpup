"""Fictional jobs use real transactions and never skip failed or overdue occurrences."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID

import pytest
from coinpup_api.business.draft_schemas import BusinessDraftCreate
from coinpup_api.business.recurring_generation import RecurringGenerationService
from coinpup_api.business.recurring_job import run_recurring
from coinpup_api.business.recurring_schemas import RecurringRuleArchive
from coinpup_api.business.schemas import ProjectUpdate
from coinpup_api.ledger.schemas import EntityUpdate
from sqlalchemy.exc import SQLAlchemyError

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_recurring_rules import create
from tests.integration.business.test_recurring_rules import rules as rules
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import financial_counts
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration
NOW = datetime(2026, 4, 1, tzinfo=UTC)


def test_bounded_backlog_and_repeat_invocations_do_not_regenerate(rules):
    s = rules
    saved = create(s)
    before = financial_counts(s["engine"])
    result = run_recurring(s["engine"], now=NOW, per_rule=2)
    assert result["confirmed"] == 2 and result["failures"] == 0
    assert result["rules"][0]["status"] == "limit"
    assert s["rules"].get_rule(s["owner"], s["ledger"], saved.id).next_index == 2
    result = run_recurring(s["engine"], now=NOW, per_rule=2)
    assert result["confirmed"] == 1 and result["rules"][0]["status"] == "not_due"
    state = snapshot(s)
    assert run_recurring(s["engine"], now=NOW)["confirmed"] == 0 and snapshot(s) == state
    assert financial_counts(s["engine"]) == before


def test_concurrent_job_invocations_confirm_one_instance_per_calendar_date(rules):
    s = rules
    saved = create(s)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_recurring(s["engine"], now=NOW), range(2)))
    assert all(result["failures"] == 0 for result in results)
    assert s["rules"].get_rule(s["owner"], s["ledger"], saved.id).next_index == 3
    rows = RecurringGenerationService(s["engine"]).list_instances(s["owner"], s["ledger"], saved.id)
    assert [row.occurrence_index for row in rows] == [0, 1, 2]
    assert len({row.id for row in rows}) == 3


def test_failed_rule_keeps_its_cursor_and_does_not_block_the_next_rule(rules):
    s = rules
    bad = create(s, id=UUID(int=1))
    body = s["payload"]().model_dump()
    body["lines"][0]["project_id"] = None
    good_source = BusinessDraftCreate(**body)
    s["service"].create_draft(s["owner"], s["ledger"], good_source)
    good = create(s, id=UUID(int=2), source_document_id=good_source.id)
    s["master"].update_project(
        s["owner"], s["ledger"], s["project"].id, ProjectUpdate(expected_version=1, archived=True)
    )
    before = financial_counts(s["engine"])
    result = run_recurring(s["engine"], now=NOW, per_rule=1)
    assert result["failures"] == 1 and result["confirmed"] == 1
    assert result["rules"][0] == dict(
        rule_id=str(bad.id), confirmed=0, status="failed", code="reference_archived"
    )
    assert s["rules"].get_rule(s["owner"], s["ledger"], bad.id).next_index == 0
    assert s["rules"].get_rule(s["owner"], s["ledger"], good.id).next_index == 1
    assert financial_counts(s["engine"]) == before


@pytest.mark.parametrize("target", ["rule", "entity"])
def test_paused_rules_are_not_scheduled_and_restore_keeps_the_first_missed_date(rules, target):
    s = rules
    saved = create(s)
    if target == "rule":
        s["rules"].archive_rule(
            s["owner"],
            s["ledger"],
            saved.id,
            RecurringRuleArchive(expected_version=1, archived=True),
        )
    else:
        s["structure"].update_entity(
            s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
        )
    before = snapshot(s)
    assert run_recurring(s["engine"], now=NOW) == dict(rules=[], confirmed=0, failures=0)
    assert snapshot(s) == before
    if target == "rule":
        s["rules"].archive_rule(
            s["owner"],
            s["ledger"],
            saved.id,
            RecurringRuleArchive(expected_version=2, archived=False),
        )
    else:
        s["structure"].update_entity(
            s["owner"], s["entity"].id, EntityUpdate(expected_version=2, archived=False)
        )
    assert run_recurring(s["engine"], now=NOW, per_rule=1)["confirmed"] == 1
    row = RecurringGenerationService(s["engine"]).get_instance(s["owner"], s["ledger"], saved.id, 0)
    assert row.scheduled_date.isoformat() == "2026-01-31"


def test_lost_committed_result_is_not_generated_again_on_the_next_invocation(rules, monkeypatch):
    s = rules
    saved = create(s)
    original = RecurringGenerationService.generate_occurrence

    def lose_result(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise SQLAlchemyError("Fictional lost committed result")

    with monkeypatch.context() as patch:
        patch.setattr(RecurringGenerationService, "generate_occurrence", lose_result)
        result = run_recurring(s["engine"], now=NOW, per_rule=1)
    assert result["failures"] == 1 and result["confirmed"] == 0
    assert s["rules"].get_rule(s["owner"], s["ledger"], saved.id).next_index == 1
    service = RecurringGenerationService(s["engine"])
    first = service.get_instance(s["owner"], s["ledger"], saved.id, 0)
    result = run_recurring(s["engine"], now=NOW, per_rule=2)
    assert result["failures"] == 0 and result["confirmed"] == 2
    assert service.get_instance(s["owner"], s["ledger"], saved.id, 0) == first
    rows = service.list_instances(s["owner"], s["ledger"], saved.id)
    assert [row.occurrence_index for row in rows] == [0, 1, 2]
    assert len({row.id for row in rows}) == 3
