"""Two real PostgreSQL writers cannot publish a smaller sequence after a cursor."""

from concurrent.futures import Future
from threading import Event, Thread
from time import monotonic
from uuid import uuid4

import pytest
from coinpup_api.ledger.models import Category
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.ledger.service import LedgerService
from coinpup_api.sync.models import ChangeLog
from coinpup_api.sync.service import ChangeService
from sqlalchemy import insert, select, text

pytestmark = pytest.mark.integration


def start_call(function, *, name):
    result = Future()

    def run():
        try:
            result.set_result(function())
        except BaseException as error:
            result.set_exception(error)

    thread = Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread, result


def test_trigger_locks_before_identity_allocation_and_reads_do_not_wait(structure_database):
    engine, owner = structure_database
    ledger = (
        LedgerService(engine)
        .create_entity(
            owner,
            EntityCreate(kind="personal", name="Fictional ordered changes", base_asset_id="USD"),
        )
        .ledger.id
    )
    service = ChangeService(engine)
    initial = service.list_changes(owner)
    cursor = initial.next_cursor
    first_id, second_id = uuid4(), uuid4()
    writer_ready, writer_done = Event(), Event()
    backend = Future()
    threads = []

    def second_write():
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("SET LOCAL statement_timeout = '8000ms'")
                backend.set_result(connection.scalar(text("SELECT pg_backend_pid()")))
                writer_ready.set()
                # Raw SQL deliberately omits the application lock: the source trigger is the
                # final ordering boundary before INSERT change_log evaluates its identity.
                connection.execute(
                    insert(Category).values(
                        id=second_id, ledger_id=ledger, kind="expense", name="Fictional writer two"
                    )
                )
                return connection.scalar(
                    select(ChangeLog.seq).where(ChangeLog.entity_id == str(second_id))
                )
        finally:
            writer_done.set()

    with engine.connect() as first:
        transaction = first.begin()
        try:
            first.execute(
                insert(Category).values(
                    id=first_id, ledger_id=ledger, kind="expense", name="Fictional writer one"
                )
            )
            first_seq = first.scalar(
                select(ChangeLog.seq).where(ChangeLog.entity_id == str(first_id))
            )
            assert first_seq > int(cursor)
            writer, result = start_call(second_write, name="second-change-writer")
            threads.append(writer)
            assert writer_ready.wait(timeout=5), "Second writer did not reach its statement"
            pid = backend.result(timeout=1)
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                deadline = monotonic() + 5
                while True:
                    waiting = observer.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE pid = :pid AND wait_event_type = 'Lock' "
                            "AND wait_event = 'advisory')"
                        ),
                        {"pid": pid},
                    )
                    if waiting:
                        break
                    assert monotonic() < deadline, "Second writer did not wait on the advisory lock"
                    assert not writer_done.wait(timeout=0.01), (
                        "Second writer finished before the first commit"
                    )
                # Observed database lock state, not elapsed time, proves the allocation boundary.
                sequence = observer.scalar(
                    text("SELECT pg_get_serial_sequence('public.change_log', 'seq')")
                )
                quoted = ".".join(
                    observer.dialect.identifier_preparer.quote(part) for part in sequence.split(".")
                )
                assert (
                    observer.exec_driver_sql(f"SELECT last_value FROM {quoted}").scalar()
                    == first_seq
                )
                assert not writer_done.is_set()
            reader, read_result = start_call(
                lambda: service.list_changes(owner, after=cursor), name="unlocked-change-reader"
            )
            threads.append(reader)
            page = read_result.result(timeout=2)
            assert page.changes == [] and page.next_cursor == cursor
            transaction.commit()
            second_seq = result.result(timeout=5)
            assert second_seq > first_seq
            combined = service.list_changes(owner, after=cursor)
            assert [change.entity_id for change in combined.changes] == [
                str(first_id),
                str(second_id),
            ]
            assert [change.seq for change in combined.changes] == [str(first_seq), str(second_seq)]
            assert combined.next_cursor == str(second_seq)
            resumed = service.list_changes(owner, after=str(first_seq))
            assert [change.entity_id for change in resumed.changes] == [str(second_id)]
            assert resumed.next_cursor == str(second_seq)
        finally:
            if transaction.is_active:
                transaction.rollback()  # Release the blocker before any finite worker join.
            for thread in threads:
                thread.join(timeout=10)
            assert all(not thread.is_alive() for thread in threads), (
                "Change test worker did not finish"
            )
