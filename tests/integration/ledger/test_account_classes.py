"""Control identities cannot leak into legacy funds or alter old money receipts."""

import runpy
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.ledger.models import Account, AccountAsset
from coinpup_api.ledger.posting_schemas import ExchangeCreate, TransferCreate
from coinpup_api.ledger.schemas import AccountUpdate
from coinpup_api.ledger.service import LedgerError
from coinpup_api.models import Base
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from tests.integration.ledger.test_posting_service_database import classified, opening
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration
CONTROLS = {
    "receivable.customer": "receivable",
    "payable.supplier": "payable",
    "intercompany.receivable": "intercompany",
    "intercompany.payable": "intercompany",
    "advance.received": "advance",
    "advance.paid": "advance",
}


def snapshot(s):
    with s["engine"].connect() as connection:
        return {
            name: connection.execute(select(table).order_by(*table.primary_key.columns))
            .mappings()
            .all()
            for name, table in sorted(Base.metadata.tables.items())
        }


def add_control(s, key="receivable.customer", **changes):
    identifier = uuid4()
    values = dict(
        id=identifier,
        ledger_id=s["ledger"],
        name="Fictional control",
        kind=None,
        account_class=CONTROLS[key],
        system_key=key,
    )
    with s["engine"].begin() as connection:
        connection.execute(insert(Account).values(values | changes))
        for asset in ("USD", "EUR"):
            connection.execute(
                insert(AccountAsset).values(
                    account_id=identifier, asset_id=asset, ledger_id=s["ledger"]
                )
            )
    return identifier


@pytest.mark.parametrize("key", CONTROLS)
def test_control_classes_are_explicit_and_excluded_from_old_funds(ledger_setup, key):
    s = ledger_setup
    before_accounts = s["structure"].list_accounts(s["owner"], s["ledger"])
    before_balances = s["posting"].balances(s["owner"], s["ledger"])
    identifier = add_control(s, key)
    assert (
        s["structure"].list_accounts(s["owner"], s["ledger"], include_archived=True)
        == before_accounts
    )
    assert s["posting"].balances(s["owner"], s["ledger"]) == before_balances
    with pytest.raises(LedgerError) as error:
        s["posting"].balances(s["owner"], s["ledger"], account_id=identifier)
    assert error.value.code == "not_found"
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        s["structure"].update_account(
            s["owner"],
            s["ledger"],
            identifier,
            AccountUpdate(expected_version=1, name="Fictional user rename"),
        )
    assert error.value.code == "not_found" and snapshot(s) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "bank"},
        {"system_key": None},
        {"system_key": "unknown"},
        {"account_class": "payable"},
        {"account_class": "money"},
        {"account_class": "unknown"},
        {"account_class": "money", "system_key": None, "kind": None},
    ],
)
def test_database_rejects_ambiguous_control_identity(ledger_setup, changes):
    s = ledger_setup
    before = snapshot(s)
    with pytest.raises(IntegrityError):
        add_control(s, **changes)
    assert snapshot(s) == before


def test_database_keeps_system_key_unique_and_identity_immutable(ledger_setup):
    s = ledger_setup
    identifier = add_control(s)
    with pytest.raises(IntegrityError):
        add_control(s)
    for statement in (
        update(Account)
        .where(Account.id == identifier)
        .values(system_key="payable.supplier", account_class="payable"),
        update(Account).where(Account.id == identifier).values(id=uuid4()),
        update(Account)
        .where(Account.id == s["account"].id)
        .values(account_class="receivable", system_key="receivable.customer", kind=None),
        delete(Account).where(Account.id == identifier),
    ):
        before = snapshot(s)
        with pytest.raises(IntegrityError) as error:
            with s["engine"].begin() as connection:
                connection.execute(statement)
        assert error.value.orig.diag.constraint_name == "ck_accounts_class_immutable"
        assert snapshot(s) == before


@pytest.mark.parametrize("kind", ["opening", "income", "expense", "transfer", "exchange"])
def test_old_commands_reject_control_accounts_without_any_writes(ledger_setup, kind):
    s = ledger_setup
    control = add_control(s)
    if kind == "opening":
        command = opening(s, account_id=control)
    elif kind in {"income", "expense"}:
        command = classified(s, kind=kind, account_id=control)
    elif kind == "transfer":
        command = TransferCreate(
            source_account_id=control,
            destination_account_id=s["account"].id,
            asset_id="USD",
            amount="1.00",
            transaction_date="2031-07-18",
        )
    else:
        command = ExchangeCreate(
            source_account_id=s["account"].id,
            source_asset_id="USD",
            source_amount="1.00",
            destination_account_id=control,
            destination_asset_id="EUR",
            destination_amount="0.90",
            transaction_date="2031-07-18",
        )
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        getattr(s["posting"], f"post_{kind}")(
            s["owner"], s["ledger"], command, "fictional-control-rejected"
        )
    assert error.value.code == "not_found" and snapshot(s) == before


def test_fee_cannot_spend_a_control_account(ledger_setup):
    s = ledger_setup
    control = add_control(s)
    command = classified(
        s,
        fees=[
            {
                "account_id": control,
                "asset_id": "USD",
                "amount": "1.00",
                "category_id": s["expenses"][0].id,
            }
        ],
    )
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        s["posting"].post_expense(s["owner"], s["ledger"], command, "fictional-control-fee")
    assert error.value.code == "not_found" and snapshot(s) == before


def migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[3]
            / "services/api/migrations/versions/20261009_0015_account_classes.py"
        )
    )


def test_money_history_survives_class_round_trip_and_original_replay(ledger_setup):
    s = ledger_setup
    command = opening(s)
    receipt = s["posting"].post_opening(s["owner"], s["ledger"], command, "fictional-old-money")
    before = snapshot(s)
    with s["engine"].begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration()["downgrade"]()
            migration()["upgrade"]()
        assert (
            connection.scalar(select(Account.account_class).where(Account.id == s["account"].id))
            == "money"
        )
    assert snapshot(s) == before
    assert (
        s["posting"].post_opening(s["owner"], s["ledger"], command, "fictional-old-money")
        == receipt
    )
    assert snapshot(s) == before


def test_system_account_history_blocks_downgrade(ledger_setup):
    s = ledger_setup
    add_control(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error:
        with s["engine"].begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration()["downgrade"]()
    assert error.value.orig.diag.constraint_name == "ck_accounts_class_downgrade"
    assert snapshot(s) == before
