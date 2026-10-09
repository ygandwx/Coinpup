"""Real commits prove occurrence replay, local dates and all-or-nothing generation."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from coinpup_api.business.draft_schemas import BusinessDraftArchive, BusinessDraftUpdate
from coinpup_api.business.recurrence_calendar import instance_id
from coinpup_api.business.recurring_generation import RecurringGenerationService
from coinpup_api.business.recurring_schemas import RecurringInstanceResponse, RecurringRuleArchive
from coinpup_api.ledger.schemas import AssetUpdate, EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_recurring_rules import create
from tests.integration.business.test_recurring_rules import rules as rules
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import financial_counts
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration
NOW = datetime(2026, 3, 31, 16, tzinfo=UTC)


def generate(s, index=0, *, now=NOW, owner=None, ledger=None):
    return RecurringGenerationService(s["engine"]).generate_occurrence(
        owner or s["owner"], ledger or s["ledger"], s["body"].id, index, now=now
    )


def test_concurrent_same_occurrence_and_retry_return_original_without_posting(rules):
    s = rules
    create(s)
    before = financial_counts(s["engine"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: generate(s), range(2)))
    assert results[0] == results[1]
    first = results[0]
    assert first.occurrence_index == 0 and first.rule_version == 1
    assert first.original_input.lines[0].quantity == "3.00"
    rule = s["rules"].get_rule(s["owner"], s["ledger"], s["body"].id)
    assert rule.next_index == 1 and rule.version == 2
    for index, day in ((1, "2026-02-28"), (2, "2026-03-31")):
        result = generate(s, index)
        assert result.scheduled_date.isoformat() == day
        assert result.rule_version == index + 1
    saved = snapshot(s)
    assert generate(s) == first and snapshot(s) == saved
    assert financial_counts(s["engine"]) == before
    service = RecurringGenerationService(s["engine"])
    rows = service.list_instances(s["owner"], s["ledger"], rule.id, limit=2, offset=1)
    assert [row.occurrence_index for row in rows] == [1, 2]
    assert service.get_instance(s["owner"], s["ledger"], rule.id, 0) == first


def test_replay_survives_human_edit_archives_disabled_asset_and_entity(rules):
    s = rules
    create(s)
    first = generate(s)
    body = first.original_input
    s["service"].update_draft(
        s["owner"],
        s["ledger"],
        first.id,
        BusinessDraftUpdate(
            expected_version=1,
            **(body.model_dump(exclude={"id"}) | {"notes": "Fictional human edit"}),
        ),
    )
    s["service"].set_draft_archived(
        s["owner"], s["ledger"], first.id, BusinessDraftArchive(expected_version=2, archived=True)
    )
    s["service"].set_draft_archived(
        s["owner"],
        s["ledger"],
        s["source"].id,
        BusinessDraftArchive(expected_version=1, archived=True),
    )
    s["rules"].archive_rule(
        s["owner"],
        s["ledger"],
        s["body"].id,
        RecurringRuleArchive(expected_version=2, archived=True),
    )
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    assert generate(s) == first and snapshot(s) == before
    saved = s["service"].get_draft(s["owner"], s["ledger"], first.id)
    assert saved.notes == "Fictional human edit" and saved.version == 3 and saved.archived


@pytest.mark.parametrize(
    "zone,anchor,before,due",
    [
        ("Asia/Shanghai", "2026-01-31", "2026-01-30T15:59:59+00:00", "2026-01-30T16:00:00+00:00"),
        (
            "America/New_York",
            "2026-03-09",
            "2026-03-09T03:59:59+00:00",
            "2026-03-09T04:00:00+00:00",
        ),
    ],
)
def test_rule_local_midnight_and_dst_not_server_timezone(rules, zone, anchor, before, due):
    s = rules
    from datetime import date

    create(s, timezone_name=zone, anchor_date=date.fromisoformat(anchor))
    saved = snapshot(s)
    with pytest.raises(LedgerError) as error:
        generate(s, now=datetime.fromisoformat(before))
    assert error.value.code == "recurrence_not_due" and snapshot(s) == saved
    result = generate(s, now=datetime.fromisoformat(due))
    assert result.scheduled_date.isoformat() == anchor


def test_pause_resume_does_not_skip_backlog_and_disabled_asset_rolls_back(rules):
    s = rules
    create(s)
    s["rules"].archive_rule(
        s["owner"],
        s["ledger"],
        s["body"].id,
        RecurringRuleArchive(expected_version=1, archived=True),
    )
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        generate(s)
    assert error.value.code == "recurrence_archived" and snapshot(s) == before
    restored = s["rules"].archive_rule(
        s["owner"],
        s["ledger"],
        s["body"].id,
        RecurringRuleArchive(expected_version=2, archived=False),
    )
    assert restored.next_index == 0
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        generate(s)
    assert error.value.code == "asset_disabled" and snapshot(s) == before
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=2, enabled=True))
    assert generate(s).scheduled_date.isoformat() == "2026-01-31"


def test_failure_after_all_flushes_rolls_back_draft_instance_cursor_and_notifications(
    rules, monkeypatch
):
    s = rules
    create(s)
    before = snapshot(s)

    def fail(*args, **kwargs):
        raise RuntimeError("Fictional lost serialization")

    monkeypatch.setattr(RecurringInstanceResponse, "model_validate", fail)
    with pytest.raises(RuntimeError, match="Fictional lost serialization"):
        generate(s)
    assert snapshot(s) == before


def test_index_gaps_and_foreign_ownership_never_generate_or_replay(rules):
    s = rules
    create(s)
    other = (
        s["structure"]
        .create_entity(
            s["owner"], EntityCreate(kind="personal", name="Fictional other", base_asset_id="USD")
        )
        .ledger.id
    )
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        generate(s, 1)
    assert error.value.code == "recurrence_progress" and snapshot(s) == before
    generate(s)
    before = snapshot(s)
    for owner, ledger in ((s["owner"], other), (uuid4(), s["ledger"])):
        with pytest.raises(LedgerError) as error:
            generate(s, owner=owner, ledger=ledger)
        assert error.value.code == "not_found"
    assert snapshot(s) == before


def test_matching_document_identity_is_not_proof_of_a_committed_occurrence(rules):
    s = rules
    create(s)
    identifier = instance_id(s["body"].id, 0)
    manual = s["payload"](id=identifier, notes="Fictional unrelated manual draft")
    s["service"].create_draft(s["owner"], s["ledger"], manual)
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        generate(s)
    assert error.value.code == "duplicate_record" and snapshot(s) == before
    assert s["rules"].get_rule(s["owner"], s["ledger"], s["body"].id).next_index == 0
    assert s["service"].get_draft(s["owner"], s["ledger"], identifier).notes == manual.notes


def test_active_rule_uses_captured_input_after_source_edit_and_archive(rules):
    s = rules
    create(s)
    s["service"].update_draft(
        s["owner"],
        s["ledger"],
        s["source"].id,
        BusinessDraftUpdate(
            expected_version=1,
            **(s["source"].model_dump(exclude={"id"}) | {"notes": "Fictional later source"}),
        ),
    )
    s["service"].set_draft_archived(
        s["owner"],
        s["ledger"],
        s["source"].id,
        BusinessDraftArchive(expected_version=2, archived=True),
    )
    result = generate(s)
    assert result.original_input.notes == s["source"].notes
    assert result.original_input.lines[0].quantity == s["source"].lines[0].quantity
    assert s["rules"].get_rule(s["owner"], s["ledger"], s["body"].id).source_version == 1
