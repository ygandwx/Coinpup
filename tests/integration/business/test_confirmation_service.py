"""Fictional multi-line confirmation transactions and permanent raw-intent replay."""

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from coinpup_api.business.confirmation_schemas import BusinessConfirmationCreate
from coinpup_api.business.confirmations import BusinessConfirmationService
from coinpup_api.business.draft_schemas import BusinessDraftCreate
from coinpup_api.business.models import BusinessDocument
from coinpup_api.business.schemas import PartyUpdate, ProjectUpdate
from coinpup_api.ledger.models import Account, CommandReceipt, JournalLine
from coinpup_api.ledger.schemas import AssetUpdate, CategoryUpdate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import select

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def test_all_seven_frozen_v1_keys_remain_reserved(legacy_v1_receipts):
    s = legacy_v1_receipts
    before = snapshot(s)
    service = BusinessConfirmationService(s["engine"])
    for case in s["cases"].values():
        with pytest.raises(LedgerError) as error:
            service.confirm(
                s["owner"],
                s["ledger"],
                uuid4(),
                BusinessConfirmationCreate(id=uuid4(), expected_version=1),
                case["key"],
            )
        assert error.value.code == "idempotency_conflict"
    assert snapshot(s) == before


@pytest.mark.parametrize("asset,amount", [("USD", "0.01"), ("BTC", "0.00000001")])
def test_exact_smallest_asset_unit(drafts, asset, amount):
    s = drafts
    saved, payload = prepare(
        s, changes={"asset_id": asset}, line_changes={"unit_price": amount, "tax_rate_percent": "0"}
    )
    submit(s, saved, payload)
    with s["engine"].connect() as c:
        rows = c.execute(select(JournalLine.amount, JournalLine.asset_id)).all()
        assert {r.asset_id for r in rows} == {asset}
        assert sorted(r.amount for r in rows) == [-Decimal(amount), Decimal(amount)]


def test_final_control_balance_overflow_preserves_first_confirmation(drafts):
    s = drafts
    first, request = prepare(
        s, line_changes={"unit_price": "99999999999999999999.99", "tax_rate_percent": "0"}
    )
    result = submit(s, first, request)
    second, next_request = prepare(s, line_changes={"unit_price": "0.01", "tax_rate_percent": "0"})
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        submit(s, second, next_request, "fictional-overflow")
    assert error.value.code == "amount_range"
    assert snapshot(s) == before
    assert submit(s, first, request) == result


@pytest.mark.parametrize("count", [0, 200])
def test_empty_rejected_and_two_hundred_lines_commit_as_one_group(drafts, count):
    s = drafts
    source = s["payload"]().model_dump()["lines"][0]
    sources = [
        source
        | {
            "id": uuid4(),
            "quantity": "1",
            "unit_price": "0.01",
            "discount_amount": "0",
            "tax_rate_percent": "0",
        }
        for _ in range(count)
    ]
    saved, payload = prepare(s, changes={"lines": sources})
    before = snapshot(s)
    if count == 0:
        with pytest.raises(LedgerError):
            submit(s, saved, payload)
        assert snapshot(s) == before
    else:
        result = submit(s, saved, payload)
        assert len(result.postings) == 200
        assert len({p.operation_id for p in result.postings}) == 200
        with s["engine"].connect() as c:
            assert len(c.execute(select(JournalLine)).all()) == 400


def test_existing_financial_key_cannot_be_reused(drafts):
    from tests.integration.ledger.test_posting_service_database import opening

    s = drafts
    original = s["posting"].post_opening(s["owner"], s["ledger"], opening(s), "fictional-confirm")
    saved, payload = prepare(s)
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        submit(s, saved, payload)
    assert error.value.code == "idempotency_conflict"
    assert snapshot(s) == before
    assert (
        s["posting"].post_opening(s["owner"], s["ledger"], opening(s), "fictional-confirm")
        == original
    )


def test_scope_route_and_confirmation_identity_cannot_be_rebound(drafts):
    from coinpup_api.ledger.schemas import EntityCreate

    s = drafts
    first, payload = prepare(s)
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional other",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        BusinessConfirmationService(s["engine"]).confirm(
            s["owner"],
            other.ledger.id,
            first.id,
            payload,
            "fictional-confirm",
        )
    assert error.value.code == "not_found"
    assert snapshot(s) == before
    submit(s, first, payload)
    second, _ = prepare(s)
    before = snapshot(s)
    for key, code in (
        ("fictional-confirm", "idempotency_conflict"),
        ("fictional-another", "duplicate_record"),
    ):
        with pytest.raises(LedgerError) as error:
            submit(s, second, payload, key)
        assert error.value.code == code
        assert snapshot(s) == before


def test_confirmation_seals_reviewed_snapshots_without_silent_refresh(drafts):
    from coinpup_api.business.confirmation_models import BusinessConfirmation

    s = drafts
    saved, payload = prepare(s)
    s["master"].update_party(
        s["owner"],
        s["ledger"],
        s["party"].id,
        PartyUpdate(expected_version=1, name="Fictional renamed party"),
    )
    s["master"].update_project(
        s["owner"],
        s["ledger"],
        s["project"].id,
        ProjectUpdate(expected_version=1, name="Fictional renamed project"),
    )
    submit(s, saved, payload)
    with s["engine"].connect() as c:
        proof = c.scalar(select(BusinessConfirmation.snapshot))
        assert proof["document"]["party_snapshot"] == saved.party_snapshot
        assert proof["lines"][0]["project_snapshot"] == saved.lines[0].project_snapshot


def prepare(s, *, kind="invoice", changes=None, line_changes=None):
    body = s["payload"](document_kind=kind).model_dump()
    body["lines"][0].update(
        quantity="1",
        unit_price="100.00",
        discount_amount="0",
        tax_rate_percent="10",
        category_id=s["income"].id if kind == "invoice" else s["expenses"][0].id,
    )
    body["lines"][0].update(line_changes or {})
    body.update(changes or {})
    saved = s["service"].create_draft(s["owner"], s["ledger"], BusinessDraftCreate(**body))
    return saved, BusinessConfirmationCreate(id=uuid4(), expected_version=saved.version)


def submit(s, document, payload, key="fictional-confirm", **kwargs):
    return BusinessConfirmationService(s["engine"]).confirm(
        s["owner"], s["ledger"], document.id, payload, key, **kwargs
    )


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_exact_gross_control_and_original_receipt(drafts, kind):
    s = drafts
    saved, payload = prepare(s, kind=kind)
    result = submit(s, saved, payload)
    with s["engine"].connect() as c:
        assert c.scalar(select(BusinessDocument.state)) == "confirmed"
        rows = c.execute(select(JournalLine.__table__)).mappings().all()
        expected = Decimal("110") if kind == "invoice" else Decimal("-110")
        assert sum(r["amount"] for r in rows if r["role"] == "account") == expected
        assert sum(r["amount"] for r in rows) == 0
        assert all(r["account_id"] != s["account"].id for r in rows)
        assert c.scalar(select(CommandReceipt.response)) == result.model_dump(mode="json")
    before = snapshot(s)
    assert submit(s, saved, payload) == result
    assert snapshot(s) == before


@pytest.mark.parametrize("different_keys", [False, True])
def test_concurrent_requests_confirm_exactly_once(drafts, different_keys):
    s = drafts
    saved, payload = prepare(s)

    def call(number):
        try:
            return submit(s, saved, payload, f"fictional-{number if different_keys else 0}")
        except LedgerError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(call, range(2)))
    successes = [r for r in results if not isinstance(r, LedgerError)]
    if different_keys:
        assert len(successes) == 1
        assert [r.code for r in results if isinstance(r, LedgerError)] == ["document_confirmed"]
    else:
        assert len(successes) == 2 and successes[0] == successes[1]
    with s["engine"].connect() as c:
        assert len(c.execute(select(CommandReceipt)).all()) == 1
        assert len(c.execute(select(JournalLine)).all()) == 2


def test_raw_intent_conflict_and_replay_after_all_availability_changes(drafts):
    s = drafts
    saved, payload = prepare(s)
    payload = BusinessConfirmationCreate(
        id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), expected_version=payload.expected_version
    )
    raw = json.dumps(payload.model_dump(mode="json") | {"id": str(payload.id).upper()}).encode()
    result = submit(s, saved, payload, raw_body=raw)
    change_period(s, cutoff="2026-10-31")
    s["master"].update_party(
        s["owner"], s["ledger"], s["party"].id, PartyUpdate(expected_version=1, archived=True)
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
    before = snapshot(s)
    assert submit(s, saved, payload, raw_body=raw) == result
    with pytest.raises(LedgerError) as error:
        submit(s, saved, payload, raw_body=json.dumps(payload.model_dump(mode="json")).encode())
    assert error.value.code == "idempotency_conflict"
    assert snapshot(s) == before


@pytest.mark.parametrize("point", ["second_journal", "receipt"])
def test_failure_rolls_back_control_creation_every_line_and_notifications(
    drafts, monkeypatch, point
):
    from coinpup_api.ledger.models import Journal
    from sqlalchemy.orm import Session

    s = drafts
    original = s["payload"]().model_dump()
    original["lines"].append(
        original["lines"][0] | {"id": uuid4(), "recognition_date": "2026-10-01"}
    )
    saved = s["service"].create_draft(s["owner"], s["ledger"], BusinessDraftCreate(**original))
    payload = BusinessConfirmationCreate(id=uuid4(), expected_version=1)
    before = snapshot(s)
    execute = Session.execute
    count = 0

    def fail(self, statement, *args, **kwargs):
        nonlocal count
        table = getattr(getattr(statement, "table", None), "name", None)
        if table == Journal.__tablename__:
            count += 1
        if (point == "second_journal" and table == Journal.__tablename__ and count == 2) or (
            point == "receipt" and table == CommandReceipt.__tablename__
        ):
            raise RuntimeError("Fictional injected failure")
        return execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", fail)
    with pytest.raises(RuntimeError, match="Fictional injected failure"):
        submit(s, saved, payload)
    assert snapshot(s) == before
    with s["engine"].connect() as c:
        assert c.scalar(select(Account.id).where(Account.system_key.is_not(None))) is None


@pytest.mark.parametrize(
    "case", ["party", "project", "category", "asset", "period", "version", "owner"]
)
def test_unavailable_source_or_scope_rolls_back_without_receipt(drafts, case):
    s = drafts
    saved, payload = prepare(s)
    if case == "party":
        s["master"].update_party(
            s["owner"], s["ledger"], s["party"].id, PartyUpdate(expected_version=1, archived=True)
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
    elif case == "asset":
        s["structure"].update_asset(
            s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False)
        )
    elif case == "period":
        change_period(s, cutoff="2026-09-30")
    elif case == "version":
        payload = BusinessConfirmationCreate(id=payload.id, expected_version=2)
    elif case == "owner":
        s = s | {"owner": uuid4()}
    before = snapshot(s)
    with pytest.raises(LedgerError):
        submit(s, saved, payload)
    assert snapshot(s) == before
