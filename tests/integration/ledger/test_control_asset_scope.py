"""Complete command asset locks precede automatic control writes (fictional data)."""

import pytest
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.models import AccountAsset, AssetRecord
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event, select, update

from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def acquire(s, assets):
    service = ControlAccounts(s["engine"])
    with service._transaction(s["owner"], write=True) as session:
        accounts = service.ensure(
            session, s["owner"], s["ledger"], {"advance.paid": {"USD"}}, lock_assets=assets
        )
        return accounts["advance.paid"].id


def test_fee_only_asset_is_locked_once_before_control_write_without_extra_link(ledger_setup):
    s = ledger_setup
    statements = []

    def capture(_conn, _cursor, sql, parameters, _context, _many):
        statements.append((sql, parameters))

    event.listen(s["engine"], "before_cursor_execute", capture)
    try:
        account = acquire(s, {"USD", "EUR"})
    finally:
        event.remove(s["engine"], "before_cursor_execute", capture)
    locks = [
        i for i, (sql, _) in enumerate(statements) if "FROM assets" in sql and "FOR SHARE" in sql
    ]
    assert len(locks) == 1
    sql, parameters = statements[locks[0]]
    assert "ORDER BY assets.asset_id" in sql and set(parameters.values()) == {"EUR", "USD"}
    advisory = next(i for i, (sql, _) in enumerate(statements) if "pg_advisory_xact_lock" in sql)
    ledger = next(
        i for i, (sql, _) in enumerate(statements) if "FROM ledgers" in sql and "FOR UPDATE" in sql
    )
    entity = next(
        i for i, (sql, _) in enumerate(statements) if "FROM entities" in sql and "FOR UPDATE" in sql
    )
    write = next(
        i for i, (sql, _) in enumerate(statements) if sql.startswith("INSERT INTO accounts")
    )
    assert advisory < ledger < entity < locks[0] < write
    with s["engine"].connect() as c:
        assert list(
            c.scalars(select(AccountAsset.asset_id).where(AccountAsset.account_id == account))
        ) == ["USD"]
    before = snapshot(s)
    assert acquire(s, {"USD", "EUR"}) == account
    assert snapshot(s) == before


@pytest.mark.parametrize("assets", [set(), {"EUR"}])
def test_incomplete_asset_scope_is_rejected_without_writes(ledger_setup, assets):
    s = ledger_setup
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        acquire(s, assets)
    assert error.value.code == "invalid_control"
    assert snapshot(s) == before


@pytest.mark.parametrize("extra", ["EUR", "FICTIONAL-MISSING"])
def test_extra_disabled_or_unknown_asset_rolls_back_control_and_notifications(ledger_setup, extra):
    s = ledger_setup
    if extra == "EUR":
        with s["engine"].begin() as c:
            c.execute(
                update(AssetRecord)
                .where(AssetRecord.asset_id == extra)
                .values(enabled=False, version=2)
            )
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        acquire(s, {"USD", extra})
    assert error.value.code == ("asset_disabled" if extra == "EUR" else "not_found")
    assert snapshot(s) == before


def test_caller_failure_rolls_back_complete_asset_scope_acquisition(ledger_setup):
    s = ledger_setup
    before = snapshot(s)
    service = ControlAccounts(s["engine"])
    with pytest.raises(RuntimeError, match="Fictional command failure"):
        with service._transaction(s["owner"], write=True) as session:
            service.ensure(
                session,
                s["owner"],
                s["ledger"],
                {"advance.paid": {"USD"}},
                lock_assets={"USD", "EUR"},
            )
            raise RuntimeError("Fictional command failure")
    assert snapshot(s) == before
