"""Real PostgreSQL receipts, local journal boundaries and concurrent repayments."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.models import FinancialOperation, Journal, JournalLine
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.period_schemas import PeriodChange
from coinpup_api.ledger.period_service import PeriodService
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.posting_schemas import OpeningCreate
from coinpup_api.ledger.schemas import AccountCreate, EntityCreate
from coinpup_api.ledger.service import LedgerError, LedgerService
from coinpup_api.settlement import service as interledger
from coinpup_api.settlement.models import InterledgerGroup
from coinpup_api.settlement.schemas import (
    InterledgerAdvanceCreate,
    InterledgerRepaymentCreate,
    InterledgerSettings,
)
from coinpup_api.sync.locking import acquire_write_lock
from coinpup_api.sync.service import ChangeService
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.ledger.test_account_classes import snapshot

pytestmark = pytest.mark.integration
BASE = "/api/v1/interledger"


@pytest.fixture
def pair(structure_database):
    engine, owner = structure_database
    ledgers, posting = LedgerService(engine), PostingService(engine)
    created = [
        ledgers.create_entity(
            owner,
            EntityCreate(
                name="Fictional " + side,
                base_asset_id="USD",
                **profile,
            ),
        ).ledger.id
        for side, profile in (
            ("advance payer", {"kind": "personal", "template_key": "personal_default"}),
            (
                "advance beneficiary",
                {
                    "kind": "company",
                    "country_code": "HK",
                    "company_type": "private_limited",
                    "template_key": "business_default",
                },
            ),
        )
    ]
    first, second = [
        ledgers.create_account(
            owner, ledger, AccountCreate(name="Fictional test bank", kind="bank", asset_ids=["USD"])
        ).id
        for ledger in created
    ]
    for ledger, account, amount in zip(
        created, (first, second), ("1000.00", "2000.00"), strict=True
    ):
        posting.post_opening(
            owner,
            ledger,
            OpeningCreate(
                account_id=account, asset_id="USD", amount=amount, transaction_date="2026-11-01"
            ),
            "fictional-opening",
        )
    payer, beneficiary = created
    category = next(
        item.id for item in ledgers.list_categories(owner, beneficiary) if item.kind == "expense"
    )
    return {
        "engine": engine,
        "owner": owner,
        "posting": posting,
        "service": interledger.InterledgerService(engine),
        "payer": payer,
        "beneficiary": beneficiary,
        "advance": {
            "payer_ledger_id": str(payer),
            "beneficiary_ledger_id": str(beneficiary),
            "payer_account_id": str(first),
            "expense_category_id": str(category),
            "asset_id": "USD",
            "amount": "100.00",
            "transaction_date": "2026-11-02",
            "description": "Fictional external purchase",
        },
        "repayment": {
            "payer_receipt_account_id": str(first),
            "beneficiary_payment_account_id": str(second),
            "amount": "100.00",
            "transaction_date": "2026-11-03",
        },
    }


def _balances(pair):
    return tuple(
        pair["posting"].balances(pair["owner"], pair[role])[0].amount
        for role in ("payer", "beneficiary")
    )


def _expense_totals(pair):
    with pair["engine"].connect() as connection:
        return dict(
            connection.execute(
                select(JournalLine.ledger_id, func.sum(JournalLine.amount))
                .where(JournalLine.role == "expense")
                .group_by(JournalLine.ledger_id)
            ).all()
        )


def _create(pair):
    service, owner = pair["service"], pair["owner"]
    service.set_enabled(owner, InterledgerSettings(enabled=True))
    return service.advance(owner, InterledgerAdvanceCreate(**pair["advance"]), "advance")


def test_optional_http_path_preserves_original_receipts_and_settles_expense_once(
    pair, authenticated_client
):
    client, _, _ = authenticated_client
    cmd = {"Idempotency-Key": "fictional-advance"}
    assert client.get(BASE + "/settings").json() == {"enabled": False}
    result = client.post(BASE + "/advances", json=pair["advance"], headers=cmd)
    assert result.status_code == 409 and result.json()["detail"]["code"] == "interledger_disabled"
    assert client.put(BASE + "/settings", json={"enabled": True}).json() == {"enabled": True}
    result = client.post(BASE + "/advances", json=pair["advance"], headers=cmd)
    assert result.status_code == 201, result.json()
    advance = result.json()
    assert advance["outstanding_amount"] == "100.00"
    assert _balances(pair) == ("900.00", "2000.00")
    assert _expense_totals(pair) == {pair["beneficiary"]: Decimal("100.00")}
    path = BASE + "/advances/" + advance["id"]
    first = client.post(
        path + "/repayments",
        json=pair["repayment"] | {"amount": "40.01"},
        headers={"Idempotency-Key": "repayment-a"},
    )
    assert first.status_code == 201, first.json()
    assert client.get(path).json()["outstanding_amount"] == "59.99"
    assert _balances(pair) == ("940.01", "1959.99")
    before = snapshot(pair)
    excluded = client.post(
        path + "/repayments",
        json=pair["repayment"] | {"amount": "60.00"},
        headers={"Idempotency-Key": "overpayment"},
    )
    assert excluded.status_code == 409 and excluded.json()["detail"]["code"] == "over_repayment"
    assert snapshot(pair) == before
    second = client.post(
        path + "/repayments",
        json=pair["repayment"] | {"amount": "59.99"},
        headers={"Idempotency-Key": "repayment-b"},
    )
    assert second.status_code == 201, second.json()
    assert _balances(pair) == ("1000.00", "1900.00")
    assert _expense_totals(pair) == {pair["beneficiary"]: Decimal("100.00")}
    assert client.put(BASE + "/settings", json={"enabled": False}).status_code == 200
    before = snapshot(pair)
    assert (
        client.post(BASE + "/advances", json=pair["advance"], headers=cmd).content == result.content
    )
    assert (
        client.post(
            path + "/repayments",
            json=pair["repayment"] | {"amount": "40.01"},
            headers={"Idempotency-Key": "repayment-a"},
        ).content
        == first.content
    )
    conflict = client.post(
        BASE + "/advances", json=pair["advance"] | {"amount": "100.0"}, headers=cmd
    )
    assert (
        conflict.status_code == 409 and conflict.json()["detail"]["code"] == "idempotency_conflict"
    )
    detail = client.get(path).json()
    assert detail["outstanding_amount"] == "0.00"
    assert {item["id"] for item in detail["repayments"]} == {
        first.json()["id"],
        second.json()["id"],
    }
    assert client.get(BASE + "/advances").json() == [
        {key: value for key, value in detail.items() if key != "repayments"}
    ]
    assert all(
        row.kind == "opening"
        for ledger in (pair["payer"], pair["beneficiary"])
        for row in pair["posting"].list_operations(pair["owner"], ledger)
    )
    assert (
        client.get(
            "/api/v1/ledgers/" + str(pair["payer"]) + "/operations/" + advance["payer_operation_id"]
        ).status_code
        == 404
    )
    assert snapshot(pair) == before
    changes = ChangeService(pair["engine"]).list_changes(pair["owner"], limit=200).changes
    groups = [row for row in changes if row.entity_type == "interledger_groups"]
    assert len(groups) == 3 and all(row.ledger_id is None for row in groups)


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"amount": "0.001"}, "amount_precision"),
        ({"amount": "0.00"}, "amount_positive"),
        ({"payer_account_id": "wrong_ledger"}, "not_found"),
        ({"beneficiary_ledger_id": "missing"}, "not_found"),
        ({"beneficiary_ledger_id": "same"}, "invalid_interledger_pair"),
    ],
)
def test_rejected_inputs_preserve_both_ledgers(pair, changes, code):
    service, owner = pair["service"], pair["owner"]
    service.set_enabled(owner, InterledgerSettings(enabled=True))
    values = {
        "wrong_ledger": pair["repayment"]["beneficiary_payment_account_id"],
        "missing": str(uuid4()),
        "same": str(pair["payer"]),
    }
    command = pair["advance"] | {key: values.get(value, value) for key, value in changes.items()}
    before = snapshot(pair)
    with pytest.raises(LedgerError, match=".") as error:
        service.advance(owner, InterledgerAdvanceCreate(**command), "failed")
    assert error.value.code == code
    assert snapshot(pair) == before


def test_second_journal_failure_rolls_back_first_journal_group_and_notifications(pair, monkeypatch):
    service, owner = pair["service"], pair["owner"]
    service.set_enabled(owner, InterledgerSettings(enabled=True))
    original = interledger.persist_posting

    def fail_second(session, owner, ledger, *args):
        original(session, owner, ledger, *args)
        if ledger == pair["beneficiary"]:
            raise RuntimeError("deliberate fictional second-side failure")

    monkeypatch.setattr(interledger, "persist_posting", fail_second)
    before = snapshot(pair)
    with pytest.raises(RuntimeError, match="second-side"):
        service.advance(
            owner, InterledgerAdvanceCreate(**pair["advance"]), "preserve-after-rollback"
        )
    assert snapshot(pair) == before


@pytest.mark.parametrize("side", ["payer", "beneficiary"])
def test_either_closed_period_blocks_new_pair_but_never_original_replay(pair, side):
    service, owner = pair["service"], pair["owner"]
    receipt = _create(pair)
    PeriodService(pair["engine"]).change(
        owner,
        pair[side],
        PeriodChange(
            action="close",
            closed_through="2026-11-03",
            expected_version=1,
            reason="Fictional test close",
        ),
        "close-before-repayment",
    )
    before = snapshot(pair)
    with pytest.raises(LedgerError) as error:
        service.repay(owner, receipt.id, InterledgerRepaymentCreate(**pair["repayment"]), "blocked")
    assert error.value.code == "period_closed"
    assert service.advance(owner, InterledgerAdvanceCreate(**pair["advance"]), "advance") == receipt
    assert snapshot(pair) == before


def test_db_keeps_sealed_pair_and_forbids_independent_update(pair):
    row = _create(pair)
    with pair["engine"].connect() as connection:
        actual = connection.execute(
            select(Journal.ledger_id, Journal.sealed, JournalLine.role, JournalLine.amount)
            .join(JournalLine, JournalLine.journal_id == Journal.id)
            .where(Journal.operation_id.in_([row.payer_operation_id, row.beneficiary_operation_id]))
        ).all()
    assert sorted(actual) == sorted(
        [
            (pair["payer"], True, "account", Decimal("-100.00")),
            (pair["payer"], True, "account", Decimal("100.00")),
            (pair["beneficiary"], True, "account", Decimal("-100.00")),
            (pair["beneficiary"], True, "expense", Decimal("100.00")),
        ]
    )
    before = snapshot(pair)
    for model, identifier, values in [
        (InterledgerGroup, row.id, {"id": row.id}),
        (FinancialOperation, row.payer_operation_id, {"status": "cancelled", "version": 2}),
    ]:
        with pytest.raises(IntegrityError) as error, pair["engine"].begin() as connection:
            connection.execute(update(model).where(model.id == identifier).values(**values))
        assert error.value.orig.diag.constraint_name == "ck_interledger_immutable"
    assert snapshot(pair) == before


def test_db_will_not_commit_even_two_balanced_journals_without_their_link(pair):
    service, owner = pair["service"], pair["owner"]
    service.set_enabled(owner, InterledgerSettings(enabled=True))
    payload = InterledgerAdvanceCreate(**pair["advance"])
    before = snapshot(pair)
    with (
        pytest.raises(IntegrityError) as error,
        Session(pair["engine"]) as session,
        session.begin(),
    ):
        acquire_write_lock(session)
        counterparts = service._locked_pair(
            session, owner, pair["payer"], pair["beneficiary"], payload
        )
        asset = asset_definition(service._assets(session, [payload.asset_id])[0])
        service._record_pair(
            session,
            owner,
            pair["payer"],
            pair["beneficiary"],
            counterparts,
            payload,
            Amount.parse(payload.amount, asset),
            payload.payer_account_id,
            expense_category=payload.expense_category_id,
        )
    assert error.value.orig.diag.constraint_name == "ck_interledger_operation_link"
    assert snapshot(pair) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount", "99.99"),
        ("amount", 100),
        ("payer_operation_id", None),
        ("transaction_date", "2026-11-01"),
    ],
)
def test_db_rejects_stored_receipt_that_disagrees_with_group(pair, field, value):
    original = _create(pair)
    with pair["engine"].connect() as connection:
        saved = (
            connection.execute(
                select(InterledgerGroup.__table__).where(InterledgerGroup.id == original.id)
            )
            .mappings()
            .one()
        )
    identifier = uuid4()
    receipt = saved["response"] | {"id": str(identifier), field: value}
    before = snapshot(pair)
    with pytest.raises(IntegrityError) as error, pair["engine"].begin() as connection:
        connection.execute(
            insert(InterledgerGroup).values(
                dict(saved)
                | {"id": identifier, "command_key": "forged-fixture", "response": receipt}
            )
        )
    assert error.value.orig.diag.constraint_name == "ck_interledger_receipt_facts"
    assert snapshot(pair) == before


def test_db_rejects_group_write_with_snapshot_that_cannot_refresh_after_shared_lock(pair):
    original = _create(pair)
    with pair["engine"].connect() as connection:
        saved = (
            connection.execute(
                select(InterledgerGroup.__table__).where(InterledgerGroup.id == original.id)
            )
            .mappings()
            .one()
        )
    before = snapshot(pair)
    with pytest.raises(IntegrityError) as error, pair["engine"].begin() as connection:
        connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        connection.execute(insert(InterledgerGroup).values(dict(saved)))
    assert error.value.orig.diag.constraint_name == "ck_interledger_write_isolation"
    assert snapshot(pair) == before


@pytest.mark.slow
def test_same_key_receipt_and_competing_capacity_serialize(pair):
    row = _create(pair)
    service, owner = pair["service"], pair["owner"]

    def run(key, amount):
        try:
            return service.repay(
                owner,
                row.id,
                InterledgerRepaymentCreate(**(pair["repayment"] | {"amount": amount})),
                key,
            )
        except LedgerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(run, ["contested", "contested"], ["30.00", "30.00"])
        assert first == second and first.amount == "30.00"
        competing = list(pool.map(run, ["shortage-one", "shortage-two"], ["50.00", "50.00"]))
    assert competing.count("over_repayment") == 1
    assert service.detail(owner, row.id).outstanding_amount == "20.00"


def test_mixed_sync_page_hydrates_cash_tombstone_and_owned_non_cash_identity(
    pair, authenticated_client
):
    client, _, _ = authenticated_client
    start = client.get("/api/v1/changes/start").json()
    device = f"/api/v1/sync/devices/{uuid4()}"
    assert client.put(device, json=start).status_code == 200
    advance = _create(pair)
    payer = str(pair["payer"])
    beneficiary = str(pair["beneficiary"])
    prefix = f"/api/v1/ledgers/{beneficiary}"
    expense = client.post(
        prefix + "/expenses",
        headers={"Idempotency-Key": "fictional-mixed-sync-expense"},
        json={
            "account_id": pair["repayment"]["beneficiary_payment_account_id"],
            "asset_id": "USD",
            "amount": "0.01",
            "transaction_date": "2026-11-04",
            "recognition_date": "2026-11-04",
            "splits": [{"category_id": pair["advance"]["expense_category_id"], "amount": "0.01"}],
        },
    )
    assert expense.status_code == 201, expense.json()
    operation = prefix + "/operations/" + expense.json()["id"]
    active = client.get(operation + "/sync-projection").json()
    assert active["status"] == "active" and active["posting"] == client.get(operation).json()
    cancelled = client.post(
        operation + "/cancellations",
        headers={"Idempotency-Key": "fictional-mixed-sync-cancellation"},
        json={"expected_version": 1, "reason": "Fictional terminal record"},
    )
    assert cancelled.status_code == 200, cancelled.json()

    until = client.get("/api/v1/changes/start").json()
    query = {"after": start["cursor"], "through": until["cursor"], "limit": 100}
    page = client.get("/api/v1/changes", params=query).json()
    before = snapshot(pair)
    ids = []
    for change in page["changes"]:
        if change["entity_type"] != "financial_operations":
            continue
        ledger, identifier = change["ledger_id"], change["entity_id"]
        ids.append(identifier)
        row = client.get(f"/api/v1/ledgers/{ledger}/operations/{identifier}/sync-projection")
        assert row.status_code == 200, row.json()
        view = row.json()
        assert (view["id"], view["ledger_id"]) == (identifier, ledger)
        assert view["version"] >= change["entity_version"]
        if identifier in {str(advance.payer_operation_id), str(advance.beneficiary_operation_id)}:
            assert view["version"] == 1 and view["status"] == "active" and view["posting"] is None
        else:
            assert view["status"] == "cancelled" and view["version"] == 2
            assert view["posting"] == cancelled.json()
    assert ids == [
        str(advance.payer_operation_id),
        str(advance.beneficiary_operation_id),
        expense.json()["id"],
        expense.json()["id"],
    ]
    for identifier in (advance.payer_operation_id, uuid4()):
        result = client.get(f"{prefix}/operations/{identifier}/sync-projection")
        assert result.status_code == 404 and result.json()["detail"]["code"] == "not_found"
    legacy = f"/api/v1/ledgers/{payer}/operations/{advance.payer_operation_id}"
    assert client.get(legacy).status_code == 404
    assert snapshot(pair) == before
    confirm = client.post(
        device + "/acknowledgements",
        json={
            "limit": 100,
            "prior": start["witness"],
            "received": page,
            "after": start["cursor"],
            "through": until["cursor"],
        },
    )
    assert confirm.status_code == 200 and confirm.json()["cursor"] == until["cursor"]
