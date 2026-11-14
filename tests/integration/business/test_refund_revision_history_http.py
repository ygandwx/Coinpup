"""Owned version directories and immutable refund receipts do not consult the current head."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from coinpup_api.business.refund_revision_history import BusinessRefundHistory
from coinpup_api.business.refund_revision_models import BusinessRefundRevision
from coinpup_api.business.refund_revision_receipts import fixed_revision_receipt
from coinpup_api.ledger.models import CommandReceipt
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event
from sqlalchemy.orm import Session

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_service import correction, submit
from tests.integration.business.test_refund_service import refund, setup_refund
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("direction", ["receipt", "payout"])
def test_version_directory_and_fixed_receipts_after_replacement_cancellation_and_close(
    drafts, authenticated_client, direction
):
    s = drafts
    client, _, _ = authenticated_client
    _, _, body = setup_refund(s, direction, "EUR", "19.37")
    original = refund(s, body)
    base = f"/api/v1/ledgers/{s['ledger']}/business-refunds/{original.id}"
    assert client.get(base + "/history?offset=1").json() == []
    cmd = correction(s, original)
    first = submit(s, original, "correct", cmd, "fictional-history-first")
    second = submit(
        s,
        original,
        "correct",
        correction(
            s,
            original,
            2,
            amount="0.01",
            payment_amount="0.01",
            allocation_return_ids=[str(i) for i in original.allocation_return_ids],
        ),
        "fictional-history-second",
    )
    last = submit(
        s,
        original,
        "cancel",
        {"expected_version": 3, "reason": "Fictional terminal cancellation"},
        "fictional-history-cancel",
    )
    change_period(s, cutoff="2026-10-05")
    before = snapshot(s)
    result = client.get(base + "/history")
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    directory = result.json()
    assert [row["version"] for row in directory] == [1, 2, 3, 4]
    assert [row["action"] for row in directory] == ["create", "correct", "correct", "cancel"]
    assert [row["reason"] for row in directory] == [
        body.reason,
        first.reason,
        second.reason,
        last.reason,
    ]
    assert [row["revision_id"] for row in directory] == [
        None,
        *[str(v.revision_id) for v in (first, second, last)],
    ]
    assert all(
        set(row) == {"revision_id", "version", "reason", "action", "created_at"}
        for row in directory
    )
    for offset, limit in ((0, 1), (0, 3), (1, 2), (3, 1), (4, 200), (100000, 1)):
        assert (
            client.get(base + f"/history?limit={limit}&offset={offset}").json()
            == directory[offset : offset + limit]
        )
    for query in ("limit=0", "limit=201", "offset=-1", "offset=100001"):
        assert client.get(base + "/history?" + query).status_code == 422
    for value in (first, second, last):
        actual = client.get(base + f"/revisions/{value.revision_id}")
        assert actual.status_code == 200 and actual.headers["cache-control"] == "no-store"
        assert actual.json() == value.model_dump(mode="json")
    assert first.previous.allocation_return_ids and not first.replacement.allocation_return_ids
    assert second.replacement.allocation_return_ids == original.allocation_return_ids
    assert last.replacement is None and last.previous == second.replacement
    assert client.get(base).json()["receipt"] == original.model_dump(mode="json")
    assert snapshot(s) == before


def test_scoped_history_reads_use_a_bounded_read_only_snapshot(drafts, authenticated_client):
    s = drafts
    client, _, _ = authenticated_client
    _, _, body = setup_refund(s)
    original = refund(s, body)
    command = correction(s, original)
    receipt = submit(s, original, "correct", command, "fictional-fixed-detail")
    reads = BusinessRefundHistory(s["engine"])
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional scoped history",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    before = snapshot(s)
    statements = []

    def capture(conn, cursor, sql, *args):
        statements.append(sql)

    for call, expected in (
        (lambda: reads.list_history(s["owner"], s["ledger"], original.id), 5),
        (lambda: reads.get_revision(s["owner"], s["ledger"], original.id, receipt.revision_id), 6),
    ):
        statements.clear()
        event.listen(s["engine"], "before_cursor_execute", capture)
        try:
            call()
        finally:
            event.remove(s["engine"], "before_cursor_execute", capture)
        assert sum(st.lstrip().upper().startswith("SELECT") for st in statements) == expected
        assert any("REPEATABLE READ, READ ONLY" in st for st in statements)
        assert not any(
            "FOR UPDATE" in st or st.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
            for st in statements
        )
    for ledger, identifier in ((other.ledger.id, original.id), (s["ledger"], uuid4())):
        base = f"/api/v1/ledgers/{ledger}/business-refunds/{identifier}"
        for suffix in ("/history", f"/revisions/{receipt.revision_id}"):
            denied = client.get(base + suffix)
            assert denied.status_code == 404 and denied.json()["detail"]["code"] == "not_found"
            assert denied.headers["cache-control"] == "no-store"
    base = f"/api/v1/ledgers/{s['ledger']}/business-refunds/{original.id}"
    assert client.get(base + f"/revisions/{uuid4()}").status_code == 404
    client.cookies.clear()
    for suffix in ("/history", f"/revisions/{receipt.revision_id}"):
        assert client.get(base + suffix).status_code == 401
    assert snapshot(s) == before


def test_single_version_read_fails_closed_on_malformed_stored_receipt(drafts):
    s = drafts
    _, _, body = setup_refund(s)
    original = refund(s, body)
    result = submit(s, original, "correct", correction(s, original), "fictional-prove-detail")
    before = snapshot(s)
    with Session(s["engine"]) as session:
        row = session.get(BusinessRefundRevision, result.revision_id)
        receipt = session.get(CommandReceipt, (s["ledger"], row.idempotency_key))
        fields = {
            k: getattr(receipt, k)
            for k in (
                "ledger_id",
                "key",
                "operation_id",
                "hash_version",
                "response_status",
                "response",
            )
        }
        malformed = deepcopy(fields["response"])
        malformed["replacement"]["allocation_return_ids"] = [str(uuid4())]
        for changed in (
            None,
            {"ledger_id": uuid4()},
            {"key": "foreign"},
            {"hash_version": 1},
            {"response_status": 201},
            {"operation_id": uuid4()},
            {"response": malformed},
            {"response": fields["response"] | {"version": 2.0}},
        ):
            candidate = None if changed is None else SimpleNamespace(**(fields | changed))
            with pytest.raises(LedgerError) as error:
                fixed_revision_receipt(session, row, candidate)
            assert (error.value.status, error.value.code) == (503, "refund_revision_integrity")
        assert fixed_revision_receipt(session, row, receipt)[0] == result
    assert snapshot(s) == before
