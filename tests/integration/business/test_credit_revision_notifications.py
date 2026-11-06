"""Stable group events allocate only after the existing global writer lock."""

from concurrent.futures import Future
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from coinpup_api.business.credit_revision_models import BusinessCreditRevision as Revision
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import insert, select, text

from tests.integration.business.test_credit_current_capacity import other_credit
from tests.integration.business.test_credit_revision_storage import revision as revision
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.sync.test_change_order_database import start_call

pytestmark = pytest.mark.integration


def test_group_notification_waits_before_sequence_allocation_and_both_rollbacks_are_clean(revision):
    s, header, _, original = revision
    state = {"original": original}
    other, _ = other_credit(s, state, original.postings[0].source_line_id, net="1")
    second = header | dict(id=uuid4(), credit_id=other.id, idempotency_key=str(uuid4()))
    before = snapshot(s)
    ready, done, backend = Event(), Event(), Future()

    def write_second():
        try:
            with s["engine"].connect() as connection:
                transaction = connection.begin()
                try:
                    connection.exec_driver_sql("SET LOCAL statement_timeout='8000ms'")
                    backend.set_result(connection.scalar(text("SELECT pg_backend_pid()")))
                    ready.set()
                    # No application lock: the actual business triggers must establish ordering.
                    connection.execute(insert(Revision).values(**second))
                    row = (
                        connection.execute(
                            select(ChangeLog.__table__).where(
                                ChangeLog.entity_type == "business_credits",
                                ChangeLog.entity_id == str(other.id),
                                ChangeLog.entity_version == 2,
                            )
                        )
                        .mappings()
                        .one()
                    )
                    assert row["owner_id"] == s["owner"] and row["change_kind"] == "upsert"
                    return row["seq"]
                finally:
                    transaction.rollback()
        finally:
            done.set()

    with s["engine"].connect() as first:
        transaction = first.begin()
        worker = None
        try:
            first.execute(insert(Revision).values(**header))
            prior_sequence = first.scalar(text("SELECT max(seq) FROM change_log"))
            worker, result = start_call(write_second, name="fictional-credit-group-writer")
            assert ready.wait(timeout=5)
            with s["engine"].connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                deadline = monotonic() + 5
                while not observer.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                        "WHERE pid=:pid AND wait_event_type='Lock' AND wait_event='advisory')"
                    ),
                    {"pid": backend.result(timeout=1)},
                ):
                    assert monotonic() < deadline
                    assert not done.wait(timeout=0.01)
                sequence = observer.scalar(
                    text("SELECT pg_get_serial_sequence('public.change_log','seq')")
                )
                quoted = ".".join(
                    observer.dialect.identifier_preparer.quote(part) for part in sequence.split(".")
                )
                assert (
                    observer.exec_driver_sql(f"SELECT last_value FROM {quoted}").scalar()
                    == prior_sequence
                )
                assert not done.is_set()
            # These deliberately incomplete candidates are rolled back, never declared committed.
            assert snapshot(s) == before
            transaction.rollback()
            assert result.result(timeout=5) > prior_sequence
        finally:
            if transaction.is_active:
                transaction.rollback()
            if worker is not None:
                worker.join(timeout=10)
                assert not worker.is_alive()
    assert snapshot(s) == before
