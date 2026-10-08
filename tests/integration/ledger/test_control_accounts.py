"""Automatic controls share the command transaction and retain separate dimension balances."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.models import Account, AccountAsset, AssetRecord
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event, select, update

from scripts.check_control_restore import seed_controls, verify_controls
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def acquire(s, requirements=None):
    service = ControlAccounts(s["engine"])
    with service._transaction(s["owner"], write=True) as session:
        accounts = service.ensure(
            session,
            s["owner"],
            s["ledger"],
            requirements or {"receivable.customer": {"USD", "EUR"}, "payable.supplier": {"USD"}},
        )
        return {key: account.id for key, account in accounts.items()}


def test_concurrent_acquisition_has_one_identity_and_one_asset_link(ledger_setup):
    s = ledger_setup
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: acquire(s), range(2)))
    assert first == second
    before = snapshot(s)
    assert acquire(s) == first and snapshot(s) == before
    with s["engine"].connect() as connection:
        assert (
            len(
                connection.execute(
                    select(AccountAsset).where(AccountAsset.account_id.in_(first.values()))
                ).all()
            )
            == 3
        )
        assert set(
            connection.scalars(select(Account.version).where(Account.id.in_(first.values())))
        ) == {1}
    acquire(s, {"payable.supplier": {"USD", "BTC"}})
    with s["engine"].connect() as connection:
        assert (
            connection.scalar(
                select(Account.version).where(Account.id == first["payable.supplier"])
            )
            == 2
        )
    assert len(s["structure"].list_accounts(s["owner"], s["ledger"])) == 1


@pytest.mark.parametrize("disabled", ["account", "link", "asset", "entity"])
def test_acquisition_never_restores_disabled_or_archived_objects(ledger_setup, disabled):
    from coinpup_api.ledger.models import Entity

    s = ledger_setup
    accounts = acquire(s)
    identifier = accounts["receivable.customer"]
    statements = {
        "account": update(Account).where(Account.id == identifier).values(archived=True, version=2),
        "link": update(AccountAsset)
        .where(AccountAsset.account_id == identifier, AccountAsset.asset_id == "USD")
        .values(enabled=False),
        "asset": update(AssetRecord)
        .where(AssetRecord.asset_id == "USD")
        .values(enabled=False, version=2),
        "entity": update(Entity)
        .where(Entity.id == s["entity"].id)
        .values(archived=True, version=2),
    }
    with s["engine"].begin() as connection:
        connection.execute(statements[disabled])
    before = snapshot(s)
    with pytest.raises(LedgerError) as failure:
        acquire(s)
    assert failure.value.code in {
        "account_archived",
        "account_asset_disabled",
        "asset_disabled",
        "entity_archived",
    }
    assert snapshot(s) == before
    # Read-only historical balances still expose their original flags and exact zero.
    rows = ControlAccounts(s["engine"]).balances(s["owner"], s["ledger"])
    assert len(rows) == 3 and all(row.amount == "0.00" for row in rows)


def test_control_failure_and_caller_failure_roll_back_all_tables(ledger_setup):
    s = ledger_setup
    before = snapshot(s)
    for owner, ledger in ((uuid4(), s["ledger"]), (s["owner"], uuid4())):
        with pytest.raises(LedgerError) as failure:
            acquire(s | {"owner": owner, "ledger": ledger})
        assert failure.value.code == "not_found"
        assert snapshot(s) == before
    service = ControlAccounts(s["engine"])
    with pytest.raises(RuntimeError, match="Fictional command failure"):
        with service._transaction(s["owner"], write=True) as session:
            service.ensure(session, s["owner"], s["ledger"], {"receivable.customer": {"USD"}})
            raise RuntimeError("Fictional command failure")
    assert snapshot(s) == before


def test_control_acquisition_locks_before_ledger_entity_and_sorted_assets(ledger_setup):
    s = ledger_setup
    statements = []

    def capture(_conn, _cursor, sql, _parameters, _context, _many):
        statements.append(sql)

    event.listen(s["engine"], "before_cursor_execute", capture)
    try:
        acquire(s)
    finally:
        event.remove(s["engine"], "before_cursor_execute", capture)
    lock = next(i for i, sql in enumerate(statements) if "pg_advisory_xact_lock" in sql)
    ledger = next(
        i for i, sql in enumerate(statements) if "FROM ledgers" in sql and "FOR UPDATE" in sql
    )
    entity = next(
        i for i, sql in enumerate(statements) if "FROM entities" in sql and "FOR UPDATE" in sql
    )
    assets = next(
        i for i, sql in enumerate(statements) if "FROM assets" in sql and "FOR SHARE" in sql
    )
    write = next(i for i, sql in enumerate(statements) if sql.startswith("INSERT INTO accounts"))
    assert lock < ledger < entity < assets < write
    assert "ORDER BY assets.asset_id" in statements[assets]


def test_control_balance_api_separates_directions_dimensions_and_preserves_restore_replay(
    authenticated_client,
):
    client, engine, owner = authenticated_client
    evidence = seed_controls(engine, owner)
    ledger = evidence["ledger"]
    url = f"/api/v1/ledgers/{ledger}/control-balances"
    expected = [row.model_dump(mode="json") for row in evidence["expected"]]
    response = client.get(url)
    assert response.status_code == 200 and response.json() == expected
    assert client.get(f"/api/v1/ledgers/{ledger}/balances").json() == []
    assert len({r["system_key"] for r in expected}) == 6
    liabilities = [r for r in expected if r["system_key"] == "payable.supplier"]
    assert sorted(r["amount"] for r in liabilities) == ["-100.00", "-40.00"]
    assert len({r["party_id"] for r in liabilities}) == 2
    for field in (
        "account_class",
        "system_key",
        "party_id",
        "document_id",
        "document_line_id",
        "counterparty_entity_id",
    ):
        value = liabilities[0][field]
        assert client.get(url, params={field: value}).json() == [
            r for r in expected if r[field] == value
        ]
    pages = [
        client.get(url, params={"limit": 1, "offset": i}).json()[0] for i in range(len(expected))
    ]
    assert pages == expected
    assert client.get(url, params={"limit": 201}).status_code == 422
    assert client.get(url, params={"account_class": "money"}).status_code == 422
    assert client.get(url.replace(str(ledger), str(uuid4()))).status_code == 404
    before = snapshot({"engine": engine})
    verify_controls(engine, owner, evidence)
    assert snapshot({"engine": engine}) == before
    client.cookies.clear()
    assert client.get(url).status_code == 401
