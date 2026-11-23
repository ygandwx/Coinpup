"""Pending Invoice intent shares the permanent ledger key namespace with financial commands."""

from concurrent.futures import Future
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.ledger.models import CommandReceipt
from coinpup_api.ledger.schemas import AccountCreate
from sqlalchemy import event, insert, select, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_snapshot_binding import company as company
from tests.integration.business.test_invoice_snapshot_binding import reject, reviewed
from tests.integration.business.test_refund_revision_headers import schema_catalog
from tests.integration.conftest import invoice_key_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ledger.test_posting_service_database import opening
from tests.integration.sync.test_change_order_database import start_call

pytestmark = pytest.mark.integration


def financial(s, key):
    account = s["structure"].create_account(
        s["owner"],
        s["ledger"],
        AccountCreate(name="Fictional namespace cash", kind="wise", asset_ids=["USD"]),
    )
    payload = opening(s | {"account": account}, "1.00")
    return payload, lambda: s["posting"].post_opening(s["owner"], s["ledger"], payload, key)


def test_existing_financial_key_cannot_be_reserved_and_original_receipt_still_replays(company):
    s = company
    values = reviewed(s)
    _, post = financial(s, values["idempotency_key"])
    saved = post()
    before = snapshot(s)
    reject(s, values, "ck_invoice_command_key")
    assert post() == saved
    assert snapshot(s) == before


def test_uncommitted_reservation_already_blocks_another_financial_receipt(company):
    s = company
    values = reviewed(s)
    _, post = financial(s, "fictional-other-key")
    post()
    with s["engine"].connect() as c:
        old = dict(
            c.execute(
                select(CommandReceipt.__table__).where(
                    CommandReceipt.key == "fictional-other-key",
                    CommandReceipt.ledger_id == s["ledger"],
                )
            )
            .mappings()
            .one()
        )
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(InvoiceReservation).values(**values))
        c.execute(insert(CommandReceipt).values(**(old | {"key": values["idempotency_key"]})))
    assert caught.value.orig.diag.constraint_name == "ck_invoice_command_key"
    assert snapshot(s) == before


def test_another_ledger_can_use_the_same_key_and_rolled_back_intent_does_not_poison_it(company):
    s = company
    values = reviewed(s)
    personal = s | {"ledger": s["entity"].ledger.id}
    _, personal_post = financial(personal, values["idempotency_key"])
    first = personal_post()
    reject(s, values, "ck_invoice_reservation_complete")
    _, company_post = financial(s, values["idempotency_key"])
    second = company_post()
    before = snapshot(s)
    assert personal_post() == first and company_post() == second
    assert snapshot(s) == before


def test_all_frozen_v1_financial_keys_remain_reserved_before_any_invoice_facts(legacy_v1_receipts):
    s = legacy_v1_receipts
    before = snapshot(s)
    for case in s["cases"].values():
        with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
            # This intentionally incomplete raw header probes the first key guard only;
            # it is never described as a valid company Invoice or a persisted reservation.
            c.execute(
                insert(InvoiceReservation).values(
                    id=uuid4(), ledger_id=s["ledger"], idempotency_key=case["key"]
                )
            )
        assert caught.value.orig.diag.constraint_name == "ck_invoice_command_key"
    assert snapshot(s) == before


@pytest.mark.parametrize("level", ["REPEATABLE READ", "SERIALIZABLE"])
def test_financial_key_writer_cannot_use_a_stale_transaction_snapshot(company, level):
    s = company
    before = snapshot(s)
    with (
        pytest.raises(IntegrityError) as caught,
        s["engine"].connect().execution_options(isolation_level=level) as c,
        c.begin(),
    ):
        c.execute(insert(CommandReceipt).values(ledger_id=s["ledger"], key="fictional-stale-key"))
    assert caught.value.orig.diag.constraint_name == "ck_invoice_command_key_isolation"
    assert snapshot(s) == before


def test_empty_invoice_key_roundtrip_keeps_existing_financial_history(company):
    s = company
    _, post = financial(s, "fictional-retained-history")
    saved = post()
    before = snapshot(s)
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        catalog = schema_catalog(c)
        invoice_key_migration()["downgrade"]()
        invoice_key_migration()["upgrade"]()
        assert schema_catalog(c) == catalog
    assert post() == saved
    assert snapshot(s) == before


@pytest.mark.parametrize("history_only", [False, True])
def test_invoice_key_history_blocks_downgrade(company, history_only):
    s = company
    values = reviewed(s)
    before = snapshot(s)
    with (
        pytest.raises(IntegrityError) as caught,
        s["engine"].begin() as c,
        Operations.context(MigrationContext.configure(c)),
    ):
        if history_only:
            c.exec_driver_sql(
                "CREATE TEMP TABLE business_invoice_reservations "
                "(id uuid,ledger_id uuid,version int) ON COMMIT DROP"
            )
            c.exec_driver_sql(
                "CREATE TRIGGER fictional_invoice_key_history AFTER INSERT "
                "ON pg_temp.business_invoice_reservations FOR EACH ROW "
                "EXECUTE FUNCTION public.coinpup_record_change()"
            )
            c.execute(
                text("INSERT INTO pg_temp.business_invoice_reservations VALUES (:id,:ledger,1)"),
                {"id": uuid4(), "ledger": s["ledger"]},
            )
        else:
            c.execute(insert(InvoiceReservation).values(**values))
        invoice_key_migration()["downgrade"]()
    assert caught.value.orig.diag.constraint_name == "ck_invoice_command_key_downgrade"
    assert snapshot(s) == before


def test_waiting_reservation_sees_financial_receipt_committed_after_it_started(company):
    s = company
    values = reviewed(s)
    _, post = financial(s, values["idempotency_key"])
    pending, release, attempting, finished = Event(), Event(), Event(), Event()
    backend = Future()
    first = second = None

    def hold_receipt(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().startswith("INSERT INTO command_receipts "):
            pending.set()
            assert release.wait(timeout=15)

    def reserve():
        try:
            with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
                c.exec_driver_sql("SET LOCAL statement_timeout='10000ms'")
                backend.set_result(c.scalar(text("SELECT pg_backend_pid()")))
                attempting.set()
                c.execute(insert(InvoiceReservation).values(**values))
            return caught.value.orig.diag.constraint_name
        finally:
            finished.set()

    event.listen(s["engine"], "after_cursor_execute", hold_receipt)
    try:
        first, posted = start_call(post, name="fictional-command-key-poster")
        assert pending.wait(timeout=5)
        second, reserved = start_call(reserve, name="fictional-command-key-reserver")
        assert attempting.wait(timeout=5)
        with s["engine"].connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
            deadline = monotonic() + 5
            while not observer.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=:pid "
                    "AND wait_event_type='Lock' AND wait_event='advisory')"
                ),
                {"pid": backend.result(timeout=1)},
            ):
                assert monotonic() < deadline
                assert not finished.wait(timeout=0.01)
            assert not finished.is_set()
        release.set()
        saved = posted.result(timeout=8)
        assert reserved.result(timeout=8) == "ck_invoice_command_key"
    finally:
        release.set()
        for worker in (first, second):
            if worker is not None:
                worker.join(timeout=15)
                assert not worker.is_alive()
        event.remove(s["engine"], "after_cursor_execute", hold_receipt)
    before = snapshot(s)
    assert post() == saved
    assert snapshot(s) == before
