"""Opt-in PostgreSQL quantity tests use transaction-local temporary tables only."""

import os
from decimal import Decimal

import pytest
from coinpup_api.config import Settings
from coinpup_api.database import Database
from coinpup_api.ledger import Amount, AmountNumeric, MoneyError, get_asset, stablecoin_asset
from sqlalchemy import Column, Integer, MetaData, Table, event, func, select
from sqlalchemy.exc import StatementError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COINPUP_RUN_DB_TESTS") != "1",
        reason="Requires an explicitly configured disposable PostgreSQL database",
    ),
]


@pytest.fixture
def connection():
    # No .env/default fallback even after the opt-in: the test target must be explicit.
    url = os.environ.get("COINPUP_DATABASE_URL")
    if not url:
        pytest.fail("Set COINPUP_DATABASE_URL explicitly for disposable PostgreSQL tests")
    database = Database(Settings(_env_file=None, environment="test", database_url=url))
    try:
        with database.engine.connect() as connection, connection.begin():
            yield connection
    finally:
        database.close()


def temporary_amount_table(connection, asset):
    table = Table(
        "coinpup_quantity_contract_probe",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("quantity", AmountNumeric(asset), nullable=False),
        prefixes=["TEMPORARY"],
        postgresql_on_commit="DROP",
    )
    table.create(connection)
    return table


@pytest.mark.parametrize(
    "code,text",
    [
        ("USD", "123.45"),
        ("GBP", "-23.40"),
        ("EUR", "90.00"),
        ("HKD", "0.01"),
        ("CNY", "0.00"),
        ("BTC", "0.12345678"),
        ("ETH", "1.000000000000000001"),
        ("XMR", "0.123456789012"),
    ],
)
def test_postgresql_exact_asset_roundtrip(connection, code, text):
    asset = get_asset(code)
    table = temporary_amount_table(connection, asset)
    original = Amount.parse(text, asset)
    connection.execute(table.insert().values(id=1, quantity=original))
    restored = connection.scalar(select(table.c.quantity).where(table.c.id == 1))
    assert restored == original
    assert restored.to_string() == text
    # Confirm the actual column typmod, not just the Python decorator's declaration.
    row = connection.exec_driver_sql(
        "SELECT numeric_precision, numeric_scale FROM information_schema.columns "
        "WHERE table_schema = (SELECT nspname FROM pg_namespace WHERE oid = pg_my_temp_schema()) "
        "AND table_name = 'coinpup_quantity_contract_probe' AND column_name = 'quantity'"
    ).one()
    assert tuple(row) == (38, 18)


@pytest.mark.parametrize("scale,text", [(0, "123"), (6, "0.000001"), (18, "0.000000000000000001")])
def test_explicit_token_precision_roundtrip(connection, scale, text):
    asset = stablecoin_asset(
        code="USDC", network="test-chain", token_reference="FictionalToken", scale=scale
    )
    table = temporary_amount_table(connection, asset)
    original = Amount.parse(text, asset)
    connection.execute(table.insert().values(id=1, quantity=original))
    assert connection.scalar(select(table.c.quantity)) == original


def test_invalid_binds_are_rejected_before_any_statement_reaches_postgresql(connection):
    asset = get_asset("USD")
    table = temporary_amount_table(connection, asset)
    connection.execute(table.insert().values(id=1, quantity=Amount.parse("1.00", asset)))
    statements = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", observe)
    try:
        for value in (
            0.1,
            True,
            "1.00",
            Decimal("1.001"),
            Decimal("0.0000000000000000001"),
            Decimal("NaN"),
            Amount.parse("1", get_asset("EUR")),
        ):
            with pytest.raises(StatementError) as error:
                connection.execute(table.insert().values(id=2, quantity=value))
            assert isinstance(error.value.orig, MoneyError)
            assert statements == []
    finally:
        event.remove(connection, "before_cursor_execute", observe)
    assert connection.scalar(select(func.count()).select_from(table)) == 1
    assert connection.scalar(select(table.c.quantity)) == Amount.parse("1.00", asset)


def test_maximum_numeric_quantity_roundtrip(connection):
    asset = get_asset("ETH")
    table = temporary_amount_table(connection, asset)
    maximum = Amount.parse("99999999999999999999.999999999999999999", asset)
    connection.execute(
        table.insert(),
        [
            {"id": 1, "quantity": maximum},
            {"id": 2, "quantity": -maximum},
        ],
    )
    assert connection.scalars(select(table.c.quantity).order_by(table.c.id)).all() == [
        maximum,
        -maximum,
    ]
    with pytest.raises(MoneyError) as error:
        maximum + Amount(asset, 1)
    assert error.value.code == "amount_range"
    assert connection.scalar(select(func.count()).select_from(table)) == 2
