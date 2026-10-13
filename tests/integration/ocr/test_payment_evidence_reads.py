"""Fictional payment review exposes complete evidence identity without financial writes."""

import pytest
from coinpup_api.business.schemas import PartyUpdate
from coinpup_api.business.settlement_reads import BusinessSettlementReads
from coinpup_api.ledger.schemas import AccountUpdate, EntityCreate
from coinpup_api.ledger.service import LedgerError
from coinpup_api.ocr.payment_confirmation_reads import PaymentEvidenceReadService
from sqlalchemy import event

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_settlement_service import pay, prepared
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("count,initial", [(1, False), (1, True), (200, True)])
def test_target_preserves_exact_old_detail_and_complete_original_receipt(
    drafts, authenticated_client, count, initial
):
    s = drafts
    payload, _ = prepared(s, count=count, initial=initial)
    receipt = pay(s, payload)
    old = BusinessSettlementReads(s["engine"]).get_payment(s["owner"], s["ledger"], receipt.id)
    before = snapshot(s)
    statements = []

    def listener(connection, cursor, statement, *args):
        statements.append(statement.lower())

    event.listen(s["engine"], "before_cursor_execute", listener)
    try:
        target = PaymentEvidenceReadService(s["engine"]).get_target(
            s["owner"], s["ledger"], receipt.id
        )
    finally:
        event.remove(s["engine"], "before_cursor_execute", listener)
    assert target.payment == old and target.payment.receipt == receipt
    assert target.operation_version == 1 and target.operation_status == "active"
    assert not any("for update" in query or "pg_advisory" in query for query in statements)
    assert any("read only" in query for query in statements)
    client, _, _ = authenticated_client
    url = f"/api/v1/ledgers/{s['ledger']}/business-payments/{receipt.id}"
    response = client.get(url + "/evidence-target")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == target.model_dump(mode="json")
    assert response.json()["payment"] == client.get(url).json() == old.model_dump(mode="json")
    assert snapshot(s) == before


def test_target_keeps_historical_archived_masters_and_rejects_other_owned_ledger(
    drafts, authenticated_client
):
    s = drafts
    payload, _ = prepared(s)
    receipt = pay(s, payload)
    s["master"].update_party(
        s["owner"], s["ledger"], s["party"].id, PartyUpdate(expected_version=1, archived=True)
    )
    s["structure"].update_account(
        s["owner"], s["ledger"], s["account"].id, AccountUpdate(expected_version=1, archived=True)
    )
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional evidence other ledger",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    before = snapshot(s)
    reads = PaymentEvidenceReadService(s["engine"])
    assert reads.get_target(s["owner"], s["ledger"], receipt.id).payment.receipt == receipt
    with pytest.raises(LedgerError) as error:
        reads.get_target(s["owner"], other.ledger.id, receipt.id)
    assert error.value.status == 404
    client, _, _ = authenticated_client
    url = f"/api/v1/ledgers/{other.ledger.id}/business-payments/{receipt.id}/evidence-target"
    assert client.get(url).status_code == 404
    assert snapshot(s) == before
