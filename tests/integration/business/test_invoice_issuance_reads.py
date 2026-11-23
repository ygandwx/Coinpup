"""Closed-stage reads: native SQL rows are visible only inside the creating transaction."""

from uuid import uuid4

import pytest
from coinpup_api.business.invoice_completion_models import InvoiceCompletion
from coinpup_api.business.invoice_issuance_reads import InvoiceIssuanceReads, _read_result
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.files.models import StoredFile
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event, insert, update
from sqlalchemy.orm import Session

from tests.integration.business.test_invoice_completions import completion
from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_snapshot_binding import company as company
from tests.integration.business.test_invoice_snapshot_binding import reviewed
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("count", [1, 200])
def test_scoped_query_observes_pending_then_complete_with_only_the_original_pair(company, count):
    s = company
    res = reviewed(s, count, logo=True, uppercase=True)
    done = completion(s, res)
    before = snapshot(s)
    with s["engine"].connect() as connection, connection.begin() as tx:
        connection.execute(insert(InvoiceReservation).values(**res))
        with Session(bind=connection, join_transaction_mode="rollback_only") as session:
            result = _read_result(session, s["owner"], s["ledger"], res["id"])
            assert result.state == "pending" and result.artifacts == ()
            assert (result.number, result.document_version) == (
                res["number"],
                res["document_version"],
            )
            for owner, ledger in [(uuid4(), s["ledger"]), (s["owner"], s["entity"].ledger.id)]:
                with pytest.raises(LedgerError) as hidden:
                    _read_result(session, owner, ledger, res["id"])
                assert (hidden.value.status, hidden.value.code) == (404, "not_found")
            with pytest.raises(LedgerError) as hidden:
                InvoiceIssuanceReads(s["engine"]).get(s["owner"], s["ledger"], res["id"])
            assert hidden.value.status == 404  # Another transaction cannot see this reservation.
        connection.execute(insert(InvoiceCompletion).values(**done))
        connection.execute(
            update(StoredFile)
            .where(StoredFile.id == done["zh_file_id"])
            .values(archived=True, title="Fictional renamed", version=2)
        )
        with Session(bind=connection, join_transaction_mode="rollback_only") as session:
            queries = []

            def observe(conn, cursor, stmt, params, context, many):
                queries.append(stmt)

            event.listen(connection, "before_cursor_execute", observe)
            try:
                result = _read_result(session, s["owner"], s["ledger"], res["id"])
            finally:
                event.remove(connection, "before_cursor_execute", observe)
            assert len(queries) == 1  # Includes both PDF metadata rows; independent of line count.
            assert result.model_dump(mode="json") == done["response"]
            assert "blob_key" not in result.model_dump_json()
        tx.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "failure,status,code",
    [
        ("owner", 404, "not_found"),
        ("ledger", 404, "not_found"),
        ("personal", 422, "invoice_requires_company"),
        ("absent", 404, "not_found"),
    ],
)
def test_service_authorizes_company_before_reading_and_never_creates_a_reservation(
    setup, failure, status, code
):
    s = setup
    owner = uuid4() if failure == "owner" else s["owner"]
    ledger = (
        uuid4()
        if failure == "ledger"
        else s["ledger"]
        if failure == "personal"
        else s["profile_ledger"]
    )
    before = snapshot(s)
    observed = []

    def capture(connection, cursor, statement, params, context, many):
        observed.append(statement)

    event.listen(s["engine"], "before_cursor_execute", capture)
    try:
        with pytest.raises(LedgerError) as caught:
            InvoiceIssuanceReads(s["engine"]).get(owner, ledger, uuid4())
    finally:
        event.remove(s["engine"], "before_cursor_execute", capture)
    assert (caught.value.status, caught.value.code) == (status, code)
    assert observed[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    assert not any("FOR UPDATE" in sql or "pg_advisory" in sql for sql in observed)
    assert sum("business_invoice_reservations" in sql for sql in observed) == int(
        failure == "absent"
    )
    assert snapshot(s) == before


def test_corrupt_loaded_completion_is_unavailable_and_never_downgraded_to_pending(company):
    s = company
    res = reviewed(s)
    done = completion(s, res)
    before = snapshot(s)
    with s["engine"].connect() as connection, connection.begin() as tx:
        connection.execute(insert(InvoiceReservation).values(**res))
        connection.execute(insert(InvoiceCompletion).values(**done))
        with Session(bind=connection, join_transaction_mode="rollback_only") as session:
            # Simulate a corrupted loaded object without changing any DB data or constraints.
            loaded = session.get(InvoiceCompletion, res["id"])
            with session.no_autoflush:
                loaded.response = loaded.response | {"state": "pending"}
                with pytest.raises(LedgerError) as caught:
                    _read_result(session, s["owner"], s["ledger"], res["id"])
                assert (caught.value.status, caught.value.code) == (
                    503,
                    "invoice_completion_integrity",
                )
                session.expire(loaded)
                assert _read_result(session, s["owner"], s["ledger"], res["id"]).state == "complete"
        tx.rollback()
    assert snapshot(s) == before
