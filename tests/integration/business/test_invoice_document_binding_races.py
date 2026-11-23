"""Current source review happens after locking, while display metadata stays historical."""

from concurrent.futures import Future
from threading import Event
from time import monotonic
from uuid import UUID

import pytest
from coinpup_api.business.document_current import BusinessDocumentCurrentReads
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.business.schemas import PartyUpdate
from coinpup_api.business.service import BusinessService
from coinpup_api.ledger.schemas import CategoryUpdate
from sqlalchemy import event, insert, select, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_document_revision_commands import correction, send
from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_snapshot_binding import company as company
from tests.integration.business.test_invoice_snapshot_binding import reject, reviewed
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.sync.test_change_order_database import start_call

pytestmark = pytest.mark.integration


def test_mutable_customer_and_category_changes_leave_frozen_invoice_evidence_unchanged(company):
    s = company
    values = reviewed(s)
    current = BusinessDocumentCurrentReads(s["engine"]).current(
        s["owner"], s["ledger"], values["document_id"]
    )
    party = current.document.party_snapshot
    BusinessService(s["engine"]).update_party(
        s["owner"],
        s["ledger"],
        UUID(party["id"]),
        PartyUpdate(
            expected_version=party["version"], name="Later fictional customer", archived=True
        ),
    )
    s["structure"].update_category(
        s["owner"],
        s["ledger"],
        s["income"].id,
        CategoryUpdate(
            expected_version=s["income"].version, name="Later fictional income", archived=True
        ),
    )
    reject(s, values, "ck_invoice_reservation_complete")


def test_waiting_reservation_reloads_the_committed_document_version_after_write_lock(company):
    s = company
    values = reviewed(s)
    current = BusinessDocumentCurrentReads(s["engine"]).current(
        s["owner"], s["ledger"], values["document_id"]
    )
    pending, release, attempting, finished = Event(), Event(), Event(), Event()
    backend = Future()
    first = second = None

    def hold_correction(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().startswith("INSERT INTO business_document_revisions "):
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

    event.listen(s["engine"], "after_cursor_execute", hold_correction)
    try:
        first, corrected = start_call(
            lambda: send(
                s, current.document, correction(current.document), "fictional-lock-correct"
            ),
            name="fictional-invoice-document-corrector",
        )
        assert pending.wait(timeout=5)
        second, reserved = start_call(reserve, name="fictional-invoice-document-reserver")
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
        assert corrected.result(timeout=8).version == current.version + 1
        assert reserved.result(timeout=8) == "ck_invoice_document_binding"
    finally:
        release.set()
        for worker in (first, second):
            if worker is not None:
                worker.join(timeout=15)
                assert not worker.is_alive()
        event.remove(s["engine"], "after_cursor_execute", hold_correction)
    with s["engine"].connect() as c:
        assert c.execute(select(InvoiceReservation.id)).all() == []
