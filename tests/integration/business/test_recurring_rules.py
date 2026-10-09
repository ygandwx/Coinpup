"""Fictional rules capture exact source versions and retain recoverable identities."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from coinpup_api.business.draft_schemas import BusinessDraftArchive, BusinessDraftUpdate
from coinpup_api.business.recurring_rules import RecurringRuleService
from coinpup_api.business.recurring_schemas import (
    RecurringRuleArchive,
    RecurringRuleCreate,
    RecurringRuleResponse,
    RecurringRuleUpdate,
)
from coinpup_api.business.schemas import PartyUpdate, ProjectUpdate
from coinpup_api.ledger.schemas import AssetUpdate, CategoryUpdate, EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from coinpup_api.sync.service import ChangeService
from sqlalchemy import event

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import financial_counts
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.fixture
def rules(drafts):
    s = drafts
    source = s["payload"]()
    s["service"].create_draft(s["owner"], s["ledger"], source)
    body = RecurringRuleCreate(
        id=uuid4(),
        name="Fictional 月末账单",
        timezone_name="Asia/Shanghai",
        anchor_date="2026-01-31",
        frequency="month",
        source_document_id=source.id,
        source_version=1,
    )
    return s | dict(source=source, body=body, rules=RecurringRuleService(s["engine"]))


def create(s, **changes):
    return s["rules"].create_rule(s["owner"], s["ledger"], s["body"].model_copy(update=changes))


def test_capture_edit_refresh_archive_and_restore_are_explicit(rules):
    s = rules
    money = financial_counts(s["engine"])
    first = create(s)
    assert first.template_input == s["source"]
    assert first.version == 1 and first.next_index == 0 and not first.archived
    new_input = s["source"].model_copy(update={"notes": "Fictional changed source"})
    s["service"].update_draft(
        s["owner"],
        s["ledger"],
        s["source"].id,
        BusinessDraftUpdate(expected_version=1, **new_input.model_dump(exclude={"id"})),
    )
    second = s["rules"].update_rule(
        s["owner"],
        s["ledger"],
        first.id,
        RecurringRuleUpdate(expected_version=1, name="Fictional renamed"),
    )
    assert second.template_input == first.template_input and second.source_version == 1
    third = s["rules"].update_rule(
        s["owner"],
        s["ledger"],
        first.id,
        RecurringRuleUpdate(
            expected_version=2,
            name=second.name,
            source_document_id=s["source"].id,
            source_version=2,
        ),
    )
    assert third.template_input == new_input and third.source_version == 2
    for version, archived in ((3, True), (4, False)):
        result = s["rules"].archive_rule(
            s["owner"],
            s["ledger"],
            first.id,
            RecurringRuleArchive(expected_version=version, archived=archived),
        )
        assert result.archived == archived and result.version == version + 1
        assert result.template_input == new_input and result.next_index == 0
    changes = ChangeService(s["engine"]).list_changes(s["owner"], limit=200).changes
    own = [c for c in changes if c.entity_type == "recurring_invoice_rules"]
    assert [c.entity_version for c in own] == [1, 2, 3, 4, 5]
    assert [c.change_kind for c in own] == ["upsert", "upsert", "upsert", "archive", "restore"]
    assert financial_counts(s["engine"]) == money


def test_concurrent_create_and_stale_updates_preserve_original(rules):
    s = rules

    def attempt():
        try:
            return create(s)
        except LedgerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert results.count("duplicate_record") == 1
    original = s["rules"].get_rule(s["owner"], s["ledger"], s["body"].id)
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        create(s, name="Fictional different intent")
    assert error.value.code == "duplicate_record" and snapshot(s) == before
    assert s["rules"].get_rule(s["owner"], s["ledger"], original.id) == original
    s["rules"].archive_rule(
        s["owner"],
        s["ledger"],
        original.id,
        RecurringRuleArchive(expected_version=1, archived=True),
    )
    before = snapshot(s)
    for expected, code in ((1, "version_conflict"), (2, "recurrence_archived")):
        with pytest.raises(LedgerError) as error:
            s["rules"].update_rule(
                s["owner"],
                s["ledger"],
                original.id,
                RecurringRuleUpdate(expected_version=expected, name="Fictional"),
            )
        assert error.value.code == code and snapshot(s) == before


@pytest.mark.parametrize("blocked", ["source", "party", "project", "category", "asset", "entity"])
def test_capture_revalidates_availability_but_historical_reads_survive(rules, blocked):
    s = rules
    first = create(s)
    if blocked == "source":
        s["service"].set_draft_archived(
            s["owner"],
            s["ledger"],
            s["source"].id,
            BusinessDraftArchive(expected_version=1, archived=True),
        )
    elif blocked == "party":
        s["master"].update_party(
            s["owner"], s["ledger"], s["party"].id, PartyUpdate(expected_version=1, archived=True)
        )
    elif blocked == "project":
        s["master"].update_project(
            s["owner"],
            s["ledger"],
            s["project"].id,
            ProjectUpdate(expected_version=1, archived=True),
        )
    elif blocked == "category":
        s["structure"].update_category(
            s["owner"],
            s["ledger"],
            s["income"].id,
            CategoryUpdate(expected_version=1, archived=True),
        )
    elif blocked == "asset":
        s["structure"].update_asset(
            s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False)
        )
    else:
        s["structure"].update_entity(
            s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
        )
    before = snapshot(s)
    with pytest.raises(LedgerError):
        create(s, id=uuid4(), source_version=2 if blocked == "source" else 1)
    assert snapshot(s) == before
    assert s["rules"].get_rule(s["owner"], s["ledger"], first.id) == first
    assert s["rules"].list_rules(s["owner"], s["ledger"]) == [first]


@pytest.mark.parametrize(
    "change,code",
    [
        ({"source_version": 2}, "version_conflict"),
        ({"source_document_id": uuid4()}, "not_found"),
        ({"timezone_name": "Fictional/Nowhere"}, "recurrence_timezone"),
    ],
)
def test_bad_source_or_timezone_rolls_back(rules, change, code):
    s = rules
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        create(s, **change)
    assert error.value.code == code and snapshot(s) == before


@pytest.mark.parametrize(
    "source,version,code", [(None, 2, "version_conflict"), ("missing", 1, "not_found")]
)
def test_failed_template_refresh_cannot_commit_a_partial_rename(rules, source, version, code):
    s = rules
    saved = create(s)
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        s["rules"].update_rule(
            s["owner"],
            s["ledger"],
            saved.id,
            RecurringRuleUpdate(
                expected_version=1,
                name="Fictional uncommitted rename",
                source_document_id=uuid4() if source else s["source"].id,
                source_version=version,
            ),
        )
    assert error.value.code == code and snapshot(s) == before
    assert s["rules"].get_rule(s["owner"], s["ledger"], saved.id) == saved


def test_bill_foreign_ledger_missing_owner_and_serialization_rollback(rules, monkeypatch):
    s = rules
    bill = s["payload"](document_kind="bill", lines=[])
    s["service"].create_draft(s["owner"], s["ledger"], bill)
    other = (
        s["structure"]
        .create_entity(
            s["owner"], EntityCreate(kind="personal", name="Fictional other", base_asset_id="USD")
        )
        .ledger.id
    )
    saved = create(s)
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        create(s, id=uuid4(), source_document_id=bill.id)
    assert error.value.code == "recurrence_template"
    for owner, ledger in ((s["owner"], other), (uuid4(), s["ledger"])):
        for operation in (
            lambda owner=owner, ledger=ledger: s["rules"].create_rule(owner, ledger, s["body"]),
            lambda owner=owner, ledger=ledger: s["rules"].get_rule(owner, ledger, saved.id),
            lambda owner=owner, ledger=ledger: s["rules"].update_rule(
                owner, ledger, saved.id, RecurringRuleUpdate(expected_version=1, name="Fictional")
            ),
            lambda owner=owner, ledger=ledger: s["rules"].archive_rule(
                owner, ledger, saved.id, RecurringRuleArchive(expected_version=1, archived=True)
            ),
        ):
            with pytest.raises(LedgerError) as error:
                operation()
            assert error.value.code == "not_found"

    def fail(*args, **kwargs):
        raise RuntimeError("Fictional serialization failure")

    monkeypatch.setattr(RecurringRuleResponse, "model_validate", fail)
    with pytest.raises(RuntimeError, match="Fictional serialization"):
        create(s, id=uuid4())
    assert snapshot(s) == before


def test_list_pagination_archive_filter_and_constant_query_count(rules):
    s = rules
    first, second = create(s), create(s, id=uuid4())
    s["rules"].archive_rule(
        s["owner"], s["ledger"], first.id, RecurringRuleArchive(expected_version=1, archived=True)
    )
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(s["engine"], "before_cursor_execute", record)
    try:
        rows = s["rules"].list_rules(s["owner"], s["ledger"])
    finally:
        event.remove(s["engine"], "before_cursor_execute", record)
    assert [r.id for r in rows] == [first.id, second.id]
    assert len([sql for sql in statements if sql.startswith("SELECT")]) == 4
    assert s["rules"].list_rules(s["owner"], s["ledger"], limit=1, offset=1) == [second]
    assert s["rules"].list_rules(s["owner"], s["ledger"], include_archived=False) == [second]
    for values in ({"limit": 0}, {"offset": -1}):
        with pytest.raises(LedgerError) as error:
            s["rules"].list_rules(s["owner"], s["ledger"], **values)
        assert error.value.code == "invalid_pagination"
