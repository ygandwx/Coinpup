"""Effective refund facts use exact current units in an owned, read-only snapshot."""

from uuid import uuid4

import pytest
from coinpup_api.business.refund_reads import BusinessRefundReads
from coinpup_api.ledger.schemas import AssetUpdate, EntityCreate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_service import correction, submit
from tests.integration.business.test_refund_service import refund, setup_refund
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ledger.test_posting_service_database import opening

pytestmark = pytest.mark.integration


def current(s, client, identifier):
    before = snapshot(s)
    response = client.get(f"/api/v1/ledgers/{s['ledger']}/business-refunds/{identifier}/current")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store" and snapshot(s) == before
    return response.json()


@pytest.mark.parametrize("direction", ["receipt", "payout"])
def test_original_correction_and_cancel_expose_only_the_effective_cash_fact(
    drafts, authenticated_client, direction
):
    s = drafts
    client, _, _ = authenticated_client
    payment, _, body = setup_refund(s, direction, "EUR", "18.37")
    original = refund(s, body)
    first = current(s, client, original.id)
    assert first["version"] == 1 and first["status"] == "active"
    assert first["revision_id"] is None and first["revision_reason"] is None
    assert first["journal_id"] == str(original.journal_id)
    assert first["payment_direction"] == direction and first["payment_id"] == str(payment.id)
    assert first["fact"]["amount"] == "18.37" and first["fact"]["payment_amount"] == "20.00"
    assert first["fact"]["allocation_return_ids"] == [
        str(i) for i in original.allocation_return_ids
    ]
    s["posting"].post_opening(
        s["owner"], s["ledger"], opening(s, "1", "BTC"), "fictional-fee-funds"
    )
    fees = [
        dict(
            account_id=str(body.account_id),
            category_id=str(s["expenses"][0].id),
            asset_id="BTC",
            amount=quantity,
        )
        for quantity in ("0.00000009", "0.00000001")
    ]
    cmd = correction(s, original, fees=fees)
    saved = submit(s, original, "correct", cmd, "fictional-current-correction")
    second = current(s, client, original.id)
    assert second["version"] == 2 and second["revision_id"] == str(saved.revision_id)
    assert second["journal_id"] == str(saved.replacement_journal_id)
    assert second["fact"]["allocation_return_ids"] == [] and second["fact"]["fees"] == fees
    assert second["fact"]["amount"] == second["fact"]["payment_amount"] == "15.00"
    assert second["fact"]["reason"] == cmd["replacement"]["reason"]
    assert second["revision_reason"] == cmd["reason"]
    # A disabled historical fee asset retains its actual precision and can be reversed.
    asset = next(a for a in s["structure"].list_assets(s["owner"]) if a.asset_id == "BTC")
    disabled = s["structure"].update_asset(
        s["owner"], "BTC", AssetUpdate(expected_version=asset.version, enabled=False)
    )
    try:
        assert current(s, client, original.id) == second
        end = {"expected_version": 2, "reason": "Fictional cancel current refund"}
        stopped = submit(s, original, "cancel", end, "fictional-current-cancel")
        third = current(s, client, original.id)
        assert third == second | dict(
            version=3,
            status="cancelled",
            revision_id=str(stopped.revision_id),
            revision_reason=end["reason"],
            journal_id=None,
            fact=None,
        )
        assert refund(s, body) == original
    finally:
        s["structure"].update_asset(
            s["owner"], "BTC", AssetUpdate(expected_version=disabled.version, enabled=True)
        )


def test_scope_authentication_and_bounded_selects_on_large_ordered_fee_list(
    drafts, authenticated_client
):
    s = drafts
    client, _, _ = authenticated_client
    _, _, body = setup_refund(s)
    original = refund(s, body)
    reads = BusinessRefundReads(s["engine"])

    def count():
        statements = []

        def track(conn, cursor, stmt, *args):
            if stmt.lstrip().upper().startswith("SELECT"):
                statements.append(stmt)

        event.listen(s["engine"], "before_cursor_execute", track)
        try:
            reads.current(s["owner"], s["ledger"], original.id)
        finally:
            event.remove(s["engine"], "before_cursor_execute", track)
        return len(statements)

    initial = count()
    fees = [
        dict(
            asset_id="USD",
            account_id=str(body.account_id),
            category_id=str(s["expenses"][0].id),
            amount="0.01",
        )
    ] * 20
    cmd = correction(s, original, fees=fees)
    saved = submit(s, original, "correct", cmd, "fictional-fee-limit")
    actual = current(s, client, original.id)
    assert actual["fact"]["fees"] == fees and len(saved.replacement.fees) == 20
    assert count() == initial == 8
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional other owner ledger",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    before = snapshot(s)
    for ledger, identifier in ((other.ledger.id, original.id), (s["ledger"], uuid4())):
        path = f"/api/v1/ledgers/{ledger}/business-refunds/{identifier}/current"
        denied = client.get(path)
        assert denied.status_code == 404 and denied.json()["detail"]["code"] == "not_found"
        assert denied.headers["cache-control"] == "no-store"
    with pytest.raises(LedgerError) as denied:
        reads.current(uuid4(), s["ledger"], original.id)
    assert (denied.value.status, denied.value.code) == (404, "not_found")
    client.cookies.clear()
    denied = client.get(f"/api/v1/ledgers/{s['ledger']}/business-refunds/{original.id}/current")
    assert denied.status_code == 401 and denied.headers["cache-control"] == "no-store"
    assert snapshot(s) == before
