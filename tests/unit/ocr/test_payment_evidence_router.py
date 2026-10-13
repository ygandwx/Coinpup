"""New readonly target endpoint keeps authenticated ownership and private error boundaries."""

from uuid import UUID

import pytest
from coinpup_api.ledger.service import LedgerError
from coinpup_api.ocr.payment_confirmation_contracts import PaymentEvidenceTarget
from coinpup_api.ocr.payment_confirmation_reads import PaymentEvidenceReadService
from sqlalchemy.exc import SQLAlchemyError

from tests.unit.business.test_settlement_router import PAYMENT, PAYMENT_RESULT
from tests.unit.ledger.test_ledger_api import LEDGER, OWNER, RECORD
from tests.unit.ledger.test_ledger_api import client as client
from tests.unit.ledger.test_ledger_api import signed_in as signed_in

URL = LEDGER + f"/business-payments/{PAYMENT['id']}/evidence-target"
RESULT = dict(
    payment=dict(
        id=PAYMENT["id"],
        ledger_id=RECORD,
        direction=PAYMENT["direction"],
        party_id=PAYMENT["party_id"],
        account_id=PAYMENT["account_id"],
        asset_id=PAYMENT["asset_id"],
        amount=PAYMENT["amount"],
        applied_amount="0",
        unapplied_amount=PAYMENT["amount"],
        transaction_date=PAYMENT["transaction_date"],
        description="Fictional target review",
        fee_count=0,
        fees=[],
        created_at="2026-10-13T00:00:00Z",
        receipt=PAYMENT_RESULT,
    ),
    operation_version=1,
    operation_status="active",
)


def test_target_requires_a_session_and_never_caches_private_reads(client):
    response = client.get(URL)
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"


def test_target_uses_authenticated_owner_ledger_and_explicit_payment_identity(
    signed_in, monkeypatch
):
    calls = []

    def capture(self, owner, ledger, payment):
        calls.append((owner, ledger, payment))
        return PaymentEvidenceTarget.model_validate(RESULT)

    monkeypatch.setattr(PaymentEvidenceReadService, "get_target", capture)
    response = signed_in.get(URL)
    assert response.status_code == 200 and response.json() == RESULT
    assert response.headers["cache-control"] == "no-store"
    assert calls == [(OWNER, UUID(RECORD), UUID(PAYMENT["id"]))]


@pytest.mark.parametrize("failure", ["missing", "storage"])
def test_target_maps_failures_without_leaking_storage_details(signed_in, monkeypatch, failure):
    def fail(*args):
        if failure == "missing":
            raise LedgerError("not_found", 404, "Record was not found.")
        raise SQLAlchemyError("Fictional private database detail")

    monkeypatch.setattr(PaymentEvidenceReadService, "get_target", fail)
    response = signed_in.get(URL)
    assert response.status_code == (404 if failure == "missing" else 503)
    assert "Fictional private" not in response.text
    assert response.headers["cache-control"] == "no-store"
