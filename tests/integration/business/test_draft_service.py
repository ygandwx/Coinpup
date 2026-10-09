"""Fictional draft transactions, ownership and unknown-submit recovery on PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from coinpup_api.business.draft_schemas import BusinessDraftCreate, BusinessDraftResponse
from coinpup_api.business.drafts import DraftService
from coinpup_api.business.models import BusinessDocument
from coinpup_api.business.schemas import PartyCreate, PartyUpdate, ProjectCreate, ProjectUpdate
from coinpup_api.business.service import BusinessService
from coinpup_api.ledger.schemas import AssetUpdate, CategoryUpdate, EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import event, insert, select, update

from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import (
    financial_counts,
)
from tests.integration.ledger.test_posting_service_database import (
    ledger_setup as ledger_setup,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def drafts(ledger_setup):
    s = ledger_setup
    master = BusinessService(s["engine"])
    party = master.create_party(
        s["owner"],
        s["ledger"],
        PartyCreate(
            id=uuid4(),
            name="Fictional 客户",
            role="both",
            email="fictional@example.invalid",
            notes="Fictional private note",
        ),
    )
    project = master.create_project(
        s["owner"],
        s["ledger"],
        ProjectCreate(
            id=uuid4(),
            name="Fictional 项目",
            notes="Fictional private note",
        ),
    )

    def payload(**changes):
        return BusinessDraftCreate(
            **(
                dict(
                    id=uuid4(),
                    document_kind="invoice",
                    party_id=party.id,
                    asset_id="USD",
                    issue_date="2026-10-09",
                    lines=[
                        dict(
                            id=uuid4(),
                            description="Fictional 服务",
                            quantity="3.00",
                            unit_price="19.99",
                            discount_amount="9.97",
                            tax_rate_percent="8.2500",
                            category_id=s["income"].id,
                            project_id=project.id,
                            recognition_date="2026-09-30",
                        )
                    ],
                )
                | changes
            )
        )

    return s | dict(
        master=master,
        party=party,
        project=project,
        payload=payload,
        service=DraftService(s["engine"]),
    )


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_create_exact_snapshots_notifications_and_no_financial_effect(drafts, kind):
    s = drafts
    before = financial_counts(s["engine"])
    body = s["payload"](document_kind=kind).model_dump()
    if kind == "bill":
        body["lines"][0]["category_id"] = s["expenses"][0].id
    payload = BusinessDraftCreate(**body)
    result = s["service"].create_draft(s["owner"], s["ledger"], payload)
    assert (result.net_amount, result.tax_amount, result.total_amount) == ("50.00", "4.13", "54.13")
    assert result.lines[0].quantity == "3.00" and result.lines[0].tax_rate_percent == "8.2500"
    assert (
        result.lines[0].id == payload.lines[0].id and result.version == result.lines[0].version == 1
    )
    assert result.party_snapshot["id"] == str(s["party"].id)
    assert result.lines[0].project_snapshot["name"] == "Fictional 项目"
    assert "notes" not in result.party_snapshot and "details" not in result.issuer_snapshot
    assert "notes" not in result.lines[0].project_snapshot
    assert s["service"].get_draft(s["owner"], s["ledger"], result.id) == result
    assert financial_counts(s["engine"]) == before
    with s["engine"].connect() as c:
        notifications = c.execute(select(ChangeLog.__table__)).mappings().all()
    assert any(
        row["entity_type"] == "business_documents"
        and row["entity_id"] == str(result.id)
        and row["entity_version"] == 1
        and row["ledger_id"] == s["ledger"]
        for row in notifications
    )
    assert any(
        row["entity_type"] == "business_document_lines"
        and row["entity_id"] == str(result.lines[0].id)
        for row in notifications
    )


def test_unknown_create_and_concurrent_retry_never_overwrite(drafts):
    s = drafts
    payload = s["payload"]()

    def create():
        try:
            return s["service"].create_draft(s["owner"], s["ledger"], payload)
        except LedgerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert results.count("duplicate_record") == 1
    original = s["service"].get_draft(s["owner"], s["ledger"], payload.id)
    before = snapshot(s)
    for retry in (payload, payload.model_copy(update={"notes": "Fictional changed intent"})):
        with pytest.raises(LedgerError, match="already exists"):
            s["service"].create_draft(s["owner"], s["ledger"], retry)
    assert snapshot(s) == before
    assert s["service"].get_draft(s["owner"], s["ledger"], payload.id) == original


@pytest.mark.parametrize("field", ["party_id", "category_id", "project_id"])
def test_foreign_references_and_missing_owner_are_rejected_atomically(drafts, field):
    s = drafts
    other = (
        s["structure"]
        .create_entity(
            s["owner"],
            EntityCreate(
                kind="personal",
                name="Fictional other",
                base_asset_id="USD",
                template_key="personal_default",
            ),
        )
        .ledger.id
    )
    if field == "party_id":
        foreign = (
            s["master"]
            .create_party(s["owner"], other, PartyCreate(id=uuid4(), name="Fictional", role="both"))
            .id
        )
    elif field == "project_id":
        foreign = (
            s["master"]
            .create_project(s["owner"], other, ProjectCreate(id=uuid4(), name="Fictional"))
            .id
        )
    else:
        foreign = s["structure"].list_categories(s["owner"], other)[0].id
    body = s["payload"]().model_dump()
    (body if field == "party_id" else body["lines"][0])[field] = foreign
    before = snapshot(s)
    for owner in (s["owner"], uuid4()):
        with pytest.raises(LedgerError) as error:
            s["service"].create_draft(owner, s["ledger"], BusinessDraftCreate(**body))
        assert error.value.code == "not_found"
    assert snapshot(s) == before
    with pytest.raises(LedgerError):
        s["service"].get_draft(s["owner"], other, body["id"])


@pytest.mark.parametrize(
    "change,code",
    [
        (dict(unit_price="0.001"), "amount_precision"),
        (dict(discount_amount="60"), "pricing_discount"),
        (dict(quantity="99999999999999999999", unit_price="100"), "amount_range"),
    ],
)
def test_invalid_pricing_rolls_back_all_tables(drafts, change, code):
    s = drafts
    body = s["payload"]().model_dump()
    body["lines"][0].update(change)
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        s["service"].create_draft(s["owner"], s["ledger"], BusinessDraftCreate(**body))
    assert error.value.code == code and snapshot(s) == before


def test_post_flush_failure_rolls_back_notifications_and_entire_draft(drafts, monkeypatch):
    s = drafts
    before = snapshot(s)

    def fail(*args, **kwargs):
        raise RuntimeError("Fictional serialization failure")

    monkeypatch.setattr(BusinessDraftResponse, "__init__", fail)
    with pytest.raises(RuntimeError, match="Fictional serialization failure"):
        s["service"].create_draft(s["owner"], s["ledger"], s["payload"]())
    assert snapshot(s) == before


def test_saved_reads_survive_master_edits_archives_and_asset_disable(drafts):
    s = drafts
    saved = s["service"].create_draft(s["owner"], s["ledger"], s["payload"]())
    s["master"].update_party(
        s["owner"],
        s["ledger"],
        s["party"].id,
        PartyUpdate(expected_version=1, name="Fictional changed", archived=True),
    )
    s["master"].update_project(
        s["owner"], s["ledger"], s["project"].id, ProjectUpdate(expected_version=1, archived=True)
    )
    s["structure"].update_category(
        s["owner"], s["ledger"], s["income"].id, CategoryUpdate(expected_version=1, archived=True)
    )
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    assert s["service"].get_draft(s["owner"], s["ledger"], saved.id) == saved
    assert s["service"].list_drafts(s["owner"], s["ledger"])[0].total_amount == "54.13"
    before = snapshot(s)
    with pytest.raises(LedgerError):
        s["service"].create_draft(s["owner"], s["ledger"], s["payload"]())
    assert snapshot(s) == before


def test_empty_bill_pagination_legacy_exclusion_and_bounded_queries(drafts):
    s = drafts
    first = s["service"].create_draft(
        s["owner"], s["ledger"], s["payload"](document_kind="bill", lines=[])
    )
    second = s["service"].create_draft(s["owner"], s["ledger"], s["payload"]())
    with s["engine"].begin() as c:
        c.execute(insert(BusinessDocument).values(id=uuid4(), ledger_id=s["ledger"]))
        c.execute(
            update(BusinessDocument)
            .where(BusinessDocument.id == first.id)
            .values(archived=True, version=2)
        )
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(s["engine"], "before_cursor_execute", record)
    try:
        rows = s["service"].list_drafts(s["owner"], s["ledger"])
    finally:
        event.remove(s["engine"], "before_cursor_execute", record)
    assert [r.id for r in rows] == [first.id, second.id]
    assert rows[0].line_count == 0 and rows[0].total_amount == "0.00"
    assert len([sql for sql in statements if sql.startswith("SELECT")]) == 6
    assert [
        r.id for r in s["service"].list_drafts(s["owner"], s["ledger"], include_archived=False)
    ] == [second.id]
    assert s["service"].list_drafts(s["owner"], s["ledger"], limit=1, offset=1) == [rows[1]]
    for page in (dict(limit=0), dict(limit=201), dict(offset=-1)):
        with pytest.raises(LedgerError):
            s["service"].list_drafts(s["owner"], s["ledger"], **page)


@pytest.mark.parametrize(
    "case,code",
    [
        ("party", "reference_archived"),
        ("project", "reference_archived"),
        ("category", "reference_archived"),
        ("role", "party_role"),
        ("kind", "category_kind"),
    ],
)
def test_unavailable_or_wrong_direction_references_reject_new_drafts(drafts, case, code):
    s = drafts
    body = s["payload"]().model_dump()
    if case in ("party", "role"):
        values = dict(archived=True) if case == "party" else dict(role="supplier")
        s["master"].update_party(
            s["owner"], s["ledger"], s["party"].id, PartyUpdate(expected_version=1, **values)
        )
    elif case == "project":
        s["master"].update_project(
            s["owner"],
            s["ledger"],
            s["project"].id,
            ProjectUpdate(expected_version=1, archived=True),
        )
    elif case == "category":
        s["structure"].update_category(
            s["owner"],
            s["ledger"],
            s["income"].id,
            CategoryUpdate(expected_version=1, archived=True),
        )
    else:
        body["lines"][0]["category_id"] = s["expenses"][0].id
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        s["service"].create_draft(s["owner"], s["ledger"], BusinessDraftCreate(**body))
    assert error.value.code == code and snapshot(s) == before


def test_reused_line_identity_rolls_back_new_parent(drafts):
    s = drafts
    original = s["service"].create_draft(s["owner"], s["ledger"], s["payload"]())
    payload = s["payload"]().model_dump()
    payload["lines"][0]["id"] = original.lines[0].id
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        s["service"].create_draft(s["owner"], s["ledger"], BusinessDraftCreate(**payload))
    assert error.value.code == "duplicate_record" and snapshot(s) == before
