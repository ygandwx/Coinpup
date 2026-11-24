"""Exact cursor, ledger isolation, bounded SQL and real corrected/cancelled history."""

from uuid import uuid4

import pytest
from coinpup_api.business.document_revision_schemas import ConfirmedDocumentCancellation
from coinpup_api.business.invoice_issuance_history import InvoiceHistoryReads
from coinpup_api.business.invoice_reservation_command import InvoiceReservationCommand
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event

from tests.integration.business.test_document_revision_commands import send
from tests.integration.business.test_invoice_issuance_http import path
from tests.integration.business.test_invoice_predecessor import advance, current, next_input
from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_reservation_command import args
from tests.integration.business.test_invoice_snapshot_binding import company as company
from tests.integration.business.test_invoice_snapshot_binding import reviewed
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def ledger_url(s):
    return path(s["ledger"])


def test_current_superseded_cancelled_and_cursor_after_new_issue_have_scoped_lightweight_reads(
    company, authenticated_client
):
    s = company
    client, engine, owner = authenticated_client
    assert s["engine"] is engine
    row = reviewed(s, 200, uppercase=True)
    url = ledger_url(s)
    svc = InvoiceReservationCommand(engine)

    def reserve(value):
        return svc.reserve(**args(s, value))

    first = reserve(row)
    one = client.get(url, params={"limit": 1})
    assert one.status_code == 200 and one.headers["cache-control"] == "no-store"
    assert (
        one.json()["next_before"] is None and one.json()["items"][0]["document_status"] == "current"
    )
    advance(s, row["document_id"])
    second = next_input(s, row, 2)
    two = reserve(second)
    response = client.get(url, params={"limit": 1}).json()
    assert response["next_before"] == "2" and [x["id"] for x in response["items"]] == [str(two.id)]
    advance(s, row["document_id"])
    third = next_input(s, second, 3)
    three = reserve(third)
    before = snapshot(s)
    next_page = client.get(url, params={"before": response["next_before"], "limit": 1}).json()
    assert next_page["next_before"] is None and [i["id"] for i in next_page["items"]] == [
        str(first.id)
    ]
    observed = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            observed.append(statement.lower())

    event.listen(engine, "before_cursor_execute", capture)
    try:
        full = InvoiceHistoryReads(engine).list(
            owner, s["ledger"], document_id=row["document_id"], limit=200
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert [i.id for i in full.items] == [three.id, two.id, first.id] and full.next_before is None
    assert all(
        i.state == "pending" and i.current_document_version == three.document_version
        for i in full.items
    )
    assert [i.document_status for i in full.items] == ["current", "superseded", "superseded"]
    history_queries = [q for q in observed if "business_invoice_reservations" in q]
    assert len(history_queries) == 1
    assert not any(
        v in q
        for q in observed
        for v in (
            "snapshot_json",
            "request_json",
            "business_invoice_completions.response,",
            "stored_files",
            "business_document_lines",
        )
    )
    assert snapshot(s) == before
    live = current(s, row["document_id"])
    send(
        s,
        live.document,
        ConfirmedDocumentCancellation(
            expected_version=live.version, reason="Fictional cancelled Invoice"
        ),
        "fictional-history-cancel",
    )
    after = snapshot(s)
    all_rows = client.get(url).json()["items"]
    assert all(
        i["document_status"] == "cancelled"
        and i["current_document_version"] > three.document_version
        for i in all_rows
    )
    assert [x["id"] for x in all_rows] == [str(three.id), str(two.id), str(first.id)]
    assert snapshot(s) == after


@pytest.mark.parametrize(
    "params",
    [
        {"before": "9223372036854775807"},
        {"before": "9223372036854775808"},
        {"before": "001"},
        {"before": "0"},
        {"before": "-1"},
        {"limit": "0"},
        {"limit": "201"},
        {"document_id": "invalid"},
    ],
)
def test_history_rejects_invalid_cursors_and_accepts_exact_bigint_limit(
    company, authenticated_client, params
):
    s = company
    client, *_ = authenticated_client
    before = snapshot(s)
    got = client.get(ledger_url(s), params=params)
    assert got.status_code == (200 if params.get("before") == "9223372036854775807" else 422)
    assert got.headers["cache-control"] == "no-store" and snapshot(s) == before


def test_unrelated_and_foreign_ledgers_do_not_leak_history(company, authenticated_client):
    s = company
    client, engine, owner = authenticated_client
    row = reviewed(s)
    InvoiceReservationCommand(engine).reserve(**args(s, row))
    other = (
        s["structure"]
        .create_entity(
            owner,
            EntityCreate(
                kind="company",
                name="Fictional other history",
                country_code="HK",
                company_type="private_limited",
                base_asset_id="USD",
            ),
        )
        .ledger.id
    )
    before = snapshot(s)
    empty = {"items": [], "next_before": None}
    assert client.get(path(other), params={"document_id": str(row["document_id"])}).json() == empty
    assert client.get(ledger_url(s), params={"document_id": str(uuid4())}).json() == empty
    assert client.get(path(uuid4())).status_code == 404
    assert client.get(path(s["entity"].ledger.id)).status_code == 422
    with pytest.raises(LedgerError) as missing:
        InvoiceHistoryReads(engine).list(uuid4(), s["ledger"])
    assert missing.value.status == 404
    client.cookies.clear()
    assert client.get(ledger_url(s)).status_code == 401 and snapshot(s) == before
