"""Dedicated payment postings, exact fees and immutable receipts on real PostgreSQL."""

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.models import CommandReceipt, Journal, JournalLine
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting_schemas import CancellationCreate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.payment_fixture import source, write
from tests.integration.conftest import business_payment_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ledger.test_posting_service_database import opening

pytestmark = pytest.mark.integration


def balance(c, account, asset):
    return c.scalar(
        select(func.sum(JournalLine.amount)).where(
            JournalLine.account_id == account, JournalLine.asset_id == asset
        )
    )


def rejected(s, payload, control, **changes):
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        write(c, s, payload, control, **changes)
    assert error.value.orig.diag.constraint_name == "ck_business_payment_shape"
    assert snapshot(s) == before


@pytest.mark.parametrize("direction", ["receipt", "payout"])
@pytest.mark.parametrize("fee_asset", ["USD", "EUR"])
def test_payment_moves_real_money_once_and_keeps_independent_fees(
    ledger_setup, direction, fee_asset
):
    s = ledger_setup
    payload, control = source(s, direction, fee_asset=fee_asset)
    with s["engine"].begin() as c:
        result = write(c, s, payload, control)
    sign = 1 if direction == "receipt" else -1
    with s["engine"].connect() as c:
        assert balance(c, s["account"].id, "USD") == Decimal(
            1000 + sign * 110 - (fee_asset == "USD")
        )
        assert balance(c, control, "USD") == Decimal(-sign * 110)
        if fee_asset == "EUR":
            assert balance(c, s["account"].id, "EUR") == Decimal("999")
        assert c.scalar(
            select(func.sum(JournalLine.amount)).where(JournalLine.role == "expense")
        ) == Decimal("1")
        assert not c.execute(
            select(JournalLine.asset_id)
            .group_by(JournalLine.asset_id)
            .having(func.sum(JournalLine.amount) != 0)
        ).all()
        assert c.scalar(select(Journal.sealed).where(Journal.id == UUID(result["journal_id"])))
        assert (
            c.scalar(
                select(CommandReceipt.response).where(
                    CommandReceipt.key == "fictional-payment-" + str(payload.id)
                )
            )
            == result
        )


@pytest.mark.parametrize("fee_count", [0, 20])
def test_empty_and_maximum_fee_components_commit_exactly(ledger_setup, fee_count):
    s = ledger_setup
    payload, control = source(s, fee_count=fee_count)
    with s["engine"].begin() as c:
        result = write(c, s, payload, control)
    with s["engine"].connect() as c:
        assert balance(c, s["account"].id, "USD") == Decimal(890 - fee_count)
        assert (
            c.scalar(
                select(func.count())
                .select_from(JournalLine)
                .where(JournalLine.journal_id == UUID(result["journal_id"]))
            )
            == 2 + fee_count * 2
        )


@pytest.mark.parametrize("asset,amount", [("BTC", "0.00000001"), ("ETH", "0.000000000000000001")])
def test_actual_crypto_minimum_units_survive_committed_payment(ledger_setup, asset, amount):
    s = ledger_setup
    payload, control = source(s, asset=asset, amount=amount, fee_count=0)
    with s["engine"].begin() as c:
        write(c, s, payload, control)
    definition = ASSETS[asset]
    with s["engine"].connect() as c:
        assert Amount.from_decimal(balance(c, control, asset), definition).to_string() == amount
        assert Amount.from_decimal(balance(c, s["account"].id, asset), definition) == (
            Amount.parse("1000", definition) - Amount.parse(amount, definition)
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": Decimal("100")},
        {"direction": "receipt"},
        {"transaction_date": date(2026, 9, 16)},
        {"description": "Fictional inconsistent source"},
    ],
)
def test_payment_fact_must_match_its_initial_journal(ledger_setup, changes):
    s = ledger_setup
    payload, control = source(s)
    rejected(s, payload, control, payment_changes=changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"recognition_date": date(2026, 9, 16)},
        {"description": "Fictional different journal"},
    ],
)
def test_payment_header_cannot_change_dates_or_description(ledger_setup, changes):
    s = ledger_setup
    payload, control = source(s)
    rejected(s, payload, control, journal_changes=changes)


@pytest.mark.parametrize(
    "case",
    ["sign", "amount", "accounts", "party", "fee_amount", "fee_party", "missing_fee", "extra_fee"],
)
def test_balanced_but_wrong_principal_or_fee_rows_roll_back(ledger_setup, case):
    s = ledger_setup
    payload, control = source(s)

    def change(rows):
        if case in {"sign", "amount"}:
            rows[0]["amount"] = Decimal("110" if case == "sign" else "-100")
            rows[1]["amount"] = Decimal("-110" if case == "sign" else "100")
        elif case == "accounts":
            rows[0]["account_id"], rows[1]["account_id"] = control, payload.account_id
        elif case == "party":
            for row in rows[:2]:
                row["party_id"] = None
        elif case == "fee_amount":
            rows[2]["amount"], rows[3]["amount"] = Decimal("-2"), Decimal("2")
        elif case == "fee_party":
            for row in rows[2:]:
                row["party_id"] = payload.party_id
        elif case == "missing_fee":
            del rows[2:]
        else:
            rows.extend(
                [
                    row | {"id": uuid4(), "line_no": row["line_no"] + 2, "component_no": 2}
                    for row in rows[2:]
                ]
            )

    rejected(s, payload, control, change_rows=change)


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount", "NaN"),
        ("amount", "1e2"),
        ("amount", "0"),
        ("amount", "-1"),
        ("amount", "0.001"),
        ("amount", "100000000000000000000"),
        ("amount", 1),
        ("account_id", "not-a-uuid"),
        ("category_id", None),
        ("extra", True),
        (None, "Fictional scalar"),
    ],
)
def test_malformed_fee_metadata_has_stable_constraint_errors(ledger_setup, field, value):
    s = ledger_setup
    payload, control = source(s)
    fee = payload.fees[0].model_dump(mode="json")
    rejected(
        s, payload, control, payment_changes={"fees": [fee | {field: value} if field else value]}
    )


@pytest.mark.parametrize(
    "changes", [{"hash_version": 1}, {"response_status": 200}, {"response": {}}]
)
def test_receipt_requires_v2_and_the_complete_original_response(ledger_setup, changes):
    s = ledger_setup
    payload, control = source(s)
    rejected(s, payload, control, receipt_changes=changes)


def test_receipt_anchor_and_dedicated_operation_require_payment_membership(ledger_setup):
    s = ledger_setup
    payload, control = source(s)
    with s["engine"].connect() as c:
        old = c.scalar(
            select(CommandReceipt.operation_id).where(CommandReceipt.key == "fictional-seed-USD")
        )
    rejected(s, payload, control, receipt_changes={"operation_id": old})
    rejected(s, payload, control, omit_payment=True)


def test_period_refusal_and_explicit_reopen_preserve_atomic_payment(ledger_setup):
    s = ledger_setup
    payload, control = source(s)
    change_period(s, cutoff="2026-09-30")
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        write(c, s, payload, control)
    assert error.value.orig.diag.constraint_name == "ck_journal_period_closed"
    assert snapshot(s) == before
    change_period(s, version=2, cutoff=None, action="reopen")
    with s["engine"].begin() as c:
        write(c, s, payload, control)


def test_legacy_reads_and_cancellation_cannot_bypass_payment_relationships(ledger_setup):
    s = ledger_setup
    payload, control = source(s)
    with s["engine"].begin() as c:
        result = write(c, s, payload, control)
    operation = UUID(result["operation_id"])
    before = snapshot(s)
    assert all(
        item.id != operation for item in s["posting"].list_operations(s["owner"], s["ledger"])
    )
    for action in (
        lambda: s["posting"].get_operation(s["owner"], s["ledger"], operation),
        lambda: s["posting"].history(s["owner"], s["ledger"], operation),
        lambda: s["posting"].cancel_operation(
            s["owner"],
            s["ledger"],
            operation,
            CancellationCreate(expected_version=1, reason="Fictional cancellation"),
            "fictional-cancel",
        ),
    ):
        with pytest.raises(LedgerError) as error:
            action()
        assert error.value.status == 404
    assert snapshot(s) == before


def test_payment_activation_round_trip_preserves_old_receipts_and_refuses_history(ledger_setup):
    s = ledger_setup
    payload, control = source(s)
    before = snapshot(s)
    migration = business_payment_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        migration["downgrade"]()
        migration["upgrade"]()
    assert snapshot(s) == before
    s["posting"].post_opening(
        s["owner"], s["ledger"], opening(s, "1000", "USD"), "fictional-seed-USD"
    )
    assert snapshot(s) == before
    with s["engine"].begin() as c:
        write(c, s, payload, control)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        with Operations.context(MigrationContext.configure(c)):
            migration["downgrade"]()
    assert error.value.orig.diag.constraint_name == "ck_business_payment_downgrade"
    assert snapshot(s) == before


def test_notification_only_history_also_prevents_payment_activation_downgrade(ledger_setup):
    s = ledger_setup
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        # Generate a fictional historical notification through the actual source trigger;
        # public payment tables remain empty and their guards remain installed.
        c.exec_driver_sql(
            "CREATE TEMP TABLE business_payments(id uuid, ledger_id uuid, version int) "
            "ON COMMIT DROP; CREATE TRIGGER fictional_history AFTER INSERT ON "
            "pg_temp.business_payments FOR EACH ROW EXECUTE FUNCTION "
            "public.coinpup_record_change()"
        )
        c.execute(
            text("INSERT INTO pg_temp.business_payments VALUES (:id,:ledger,1)"),
            {"id": uuid4(), "ledger": s["ledger"]},
        )
        with Operations.context(MigrationContext.configure(c)):
            business_payment_migration()["downgrade"]()
    assert error.value.orig.diag.constraint_name == "ck_business_payment_downgrade"
    assert snapshot(s) == before
