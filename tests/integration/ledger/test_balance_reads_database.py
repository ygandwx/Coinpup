"""Balance snapshots remain coherent without blocking ordinary ledger writers."""

from concurrent.futures import Future
from threading import Event, Thread, current_thread
from uuid import uuid4

import pytest
from coinpup_api.ledger.models import Ledger
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.posting_schemas import ExpenseCreate, OpeningCreate
from coinpup_api.ledger.schemas import AccountCreate, AccountUpdate, EntityCreate
from coinpup_api.ledger.service import LedgerError, LedgerService
from sqlalchemy import event, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


@pytest.fixture
def balance_setup(structure_database):
    engine, owner = structure_database
    structure, posting = LedgerService(engine), PostingService(engine)
    entity = structure.create_entity(
        owner,
        EntityCreate(
            kind="personal",
            name="Fictional balance snapshot",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    ledger = entity.ledger.id
    account = structure.create_account(
        owner, ledger, AccountCreate(name="Fictional bank", kind="bank", asset_ids=["USD", "EUR"])
    )
    category = next(
        item for item in structure.list_categories(owner, ledger) if item.kind == "expense"
    )
    posting.post_opening(
        owner,
        ledger,
        OpeningCreate(
            account_id=account.id, asset_id="USD", amount="100", transaction_date="2026-01-01"
        ),
        "snapshot-opening",
    )
    expense = ExpenseCreate(
        account_id=account.id,
        asset_id="USD",
        amount="25",
        transaction_date="2026-01-02",
        recognition_date="2026-01-02",
        splits=[{"category_id": category.id, "amount": "25"}],
    )
    return engine, owner, ledger, account, posting, structure, expense


def start_call(function, *, name):
    """Surface worker failures and use finite joins, including assertion-failure paths."""
    result = Future()

    def run():
        try:
            result.set_result(function())
        except BaseException as error:
            result.set_exception(error)

    thread = Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread, result


def join_calls(*threads):
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads), "Database test worker did not finish"


def test_balances_return_while_another_connection_holds_ledger_and_entity_locks(balance_setup):
    engine, owner, ledger, _, posting, _, _ = balance_setup
    expected = posting.balances(owner, ledger)
    with Session(engine) as writer, writer.begin():
        # Use the actual write lock sequence and retain both locks until the reader finishes.
        posting._locked_ledger(writer, owner, ledger)
        reader, result = start_call(
            lambda: posting.balances(owner, ledger), name="nonblocking-balance-reader"
        )
        try:
            assert result.result(timeout=2) == expected
            assert writer.in_transaction()
        finally:
            # Release the blocker before joining even if the old locking read regresses.
            writer.rollback()
            join_calls(reader)


def test_metadata_and_totals_use_one_snapshot_while_a_writer_commits(balance_setup):
    engine, owner, ledger, account, posting, structure, expense = balance_setup
    expected = posting.balances(owner, ledger)
    metadata_read, resume_reader = Event(), Event()
    threads = []

    def pause_after_metadata(connection, cursor, statement, parameters, context, executemany):
        if (
            current_thread().name == "snapshot-balance-reader"
            and "FROM account_assets JOIN accounts" in statement
        ):
            metadata_read.set()
            assert resume_reader.wait(timeout=10), "Snapshot reader was not released"

    def write_between_reads():
        posting.post_expense(owner, ledger, expense, "snapshot-expense")
        structure.update_account(
            owner, ledger, account.id, AccountUpdate(expected_version=1, archived=True)
        )

    event.listen(engine, "after_cursor_execute", pause_after_metadata)
    try:
        reader, read_result = start_call(
            lambda: posting.balances(owner, ledger), name="snapshot-balance-reader"
        )
        threads.append(reader)
        assert metadata_read.wait(timeout=5), "Reader did not reach the metadata query"
        writer, write_result = start_call(write_between_reads, name="snapshot-ledger-writer")
        threads.append(writer)
        # The writer must commit while the reader is paused, not after its transaction closes.
        write_result.result(timeout=5)
        resume_reader.set()
        assert read_result.result(timeout=5) == expected
    finally:
        resume_reader.set()
        try:
            join_calls(*threads)
        finally:
            event.remove(engine, "after_cursor_execute", pause_after_metadata)

    current = posting.balances(owner, ledger)
    assert {item.asset_id: item.amount for item in current} == {"EUR": "0.00", "USD": "75.00"}
    assert all(item.account_archived for item in current)
    assert all(not item.account_archived for item in expected)


def test_read_only_snapshot_is_transaction_local_and_normal_writes_still_work(balance_setup):
    engine, owner, ledger, _, posting, _, expense = balance_setup
    with posting._transaction(owner, read_only=True) as session:
        backend = session.scalar(text("SELECT pg_backend_pid()"))
        assert session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert session.scalar(text("SHOW transaction_read_only")) == "on"

    with pytest.raises(DBAPIError) as rejected:
        with posting._transaction(owner, read_only=True) as session:
            assert session.scalar(text("SELECT pg_backend_pid()")) == backend
            session.execute(update(Ledger).where(Ledger.id == ledger).values(version=2))
    assert rejected.value.orig.sqlstate == "25006"

    # This fixture has used only one pooled connection: verify reuse after commit and rollback.
    with posting._transaction(owner) as session:
        assert session.scalar(text("SELECT pg_backend_pid()")) == backend
        assert session.scalar(text("SHOW transaction_read_only")) == "off"
        assert session.scalar(text("SHOW transaction_isolation")) == session.scalar(
            text("SHOW default_transaction_isolation")
        )
        assert session.get(Ledger, ledger).version == 1
    receipt = posting.post_expense(owner, ledger, expense, "write-after-snapshot")
    assert receipt.amount == "25.00"
    assert {item.asset_id: item.amount for item in posting.balances(owner, ledger)} == {
        "EUR": "0.00",
        "USD": "75.00",
    }


def test_balance_snapshot_preserves_owner_and_account_scope(balance_setup):
    _, owner, ledger, account, posting, structure, _ = balance_setup
    other = structure.create_entity(
        owner, EntityCreate(kind="personal", name="Fictional other ledger", base_asset_id="USD")
    )
    for requested_owner, requested_ledger, requested_account in [
        (uuid4(), ledger, None),
        (owner, uuid4(), None),
        (owner, other.ledger.id, account.id),
    ]:
        with pytest.raises(LedgerError) as error:
            posting.balances(requested_owner, requested_ledger, requested_account)
        assert error.value.code == "not_found" and error.value.status == 404
    assert posting.balances(owner, other.ledger.id) == []
    assert len(posting.balances(owner, ledger, account.id)) == 2
