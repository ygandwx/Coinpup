"""Observe advisory waits to prove both closing/posting commit orders."""

from concurrent.futures import Future
from contextlib import contextmanager
from threading import Event, Thread, current_thread
from time import monotonic

import pytest
from coinpup_api.ledger import service as services
from coinpup_api.ledger.period_schemas import PeriodChange
from coinpup_api.ledger.period_service import PeriodService
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import text

from tests.integration.ledger.test_posting_service_database import classified
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("close_first", [True, False])
def test_close_and_post_serialize_before_reading_period(ledger_setup, monkeypatch, close_first):
    s = ledger_setup
    periods = PeriodService(s["engine"])
    ready, release, blocked = Event(), Event(), Future()
    first_service = periods if close_first else s["posting"]
    original = first_service._transaction

    @contextmanager
    def held(*args, **kwargs):
        with original(*args, **kwargs) as session:
            yield session
            ready.set()
            assert release.wait(10), "Fictional first transaction was not released"

    monkeypatch.setattr(first_service, "_transaction", held)
    acquire = services.acquire_write_lock

    def observe(session):
        if current_thread().name == "fictional-period-second":
            session.execute(text("SET LOCAL statement_timeout = '10000ms'"))
            blocked.set_result(session.scalar(text("SELECT pg_backend_pid()")))
        acquire(session)

    monkeypatch.setattr(services, "acquire_write_lock", observe)

    def close():
        return periods.change(
            s["owner"],
            s["ledger"],
            PeriodChange(
                action="close",
                closed_through="2026-01-31",
                expected_version=1,
                reason="Fictional serialized close",
            ),
            "fictional-close",
        )

    def post():
        return s["posting"].post_expense(s["owner"], s["ledger"], classified(s), "fictional-post")

    calls = [close, post] if close_first else [post, close]
    replies = [Future(), Future()]
    threads = []

    def run(index):
        try:
            replies[index].set_result(calls[index]())
        except BaseException as error:
            replies[index].set_exception(error)

    try:
        for index, name in enumerate(["fictional-period-first", "fictional-period-second"]):
            thread = Thread(target=run, args=(index,), name=name, daemon=True)
            threads.append(thread)
            thread.start()
            if index == 0:
                assert ready.wait(5)
        pid = blocked.result(timeout=5)
        with s["engine"].connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
            deadline = monotonic() + 5
            while not observer.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE pid=:pid AND wait_event_type='Lock' AND wait_event='advisory')"
                ),
                {"pid": pid},
            ):
                assert monotonic() < deadline and not replies[1].done()
                Event().wait(0.01)
        release.set()
        replies[0].result(timeout=5)
        if close_first:
            with pytest.raises(LedgerError) as failure:
                replies[1].result(timeout=5)
            assert failure.value.code == "period_closed"
            assert s["posting"].list_operations(s["owner"], s["ledger"]) == []
        else:
            assert replies[1].result(timeout=5).version == 2
            assert len(s["posting"].list_operations(s["owner"], s["ledger"])) == 1
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=12)
            assert not thread.is_alive()
