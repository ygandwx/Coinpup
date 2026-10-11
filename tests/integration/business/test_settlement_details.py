"""Settlement details keep immutable mappings and complete original receipt identity."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from coinpup_api.business.settlement_reads import BusinessSettlementReads
from coinpup_api.ledger.models import CommandReceipt
from coinpup_api.ledger.service import LedgerError
from sqlalchemy.orm import Session

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_settlement_service import allocate, pay, prepared
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("initial", [True, False])
@pytest.mark.parametrize("asset", ["USD", "EUR"])
def test_detail_has_original_receipt_and_separate_asset_quantities(drafts, initial, asset):
    s = drafts
    body, group = prepared(s, asset=asset, initial=initial)
    payment = pay(s, body)
    allocation = payment.initial_allocation if initial else allocate(s, group)
    reads = BusinessSettlementReads(s["engine"])
    before = snapshot(s)
    detail = reads.get_payment(s["owner"], s["ledger"], payment.id)
    assert detail.receipt == payment and detail.unapplied_amount == "0.00"
    assert detail.fees[0].amount == "1.00" and detail.fee_count == 1
    detail = reads.get_allocation(s["owner"], s["ledger"], allocation.id)
    assert detail.receipt == (payment if initial else allocation)
    assert detail.is_initial == initial
    (line,) = detail.lines
    assert line.document_asset_id == asset and line.document_amount == "100.00"
    assert line.payment_amount == ("110.00" if asset == "EUR" else "100.00")
    assert line.operation_id == allocation.postings[0].operation_id
    assert line.project_id == s["project"].id and line.description
    assert snapshot(s) == before


@pytest.mark.parametrize("failure", ["shape", "anchor", "initial"])
def test_inconsistent_receipt_fails_closed_without_writing(drafts, monkeypatch, failure):
    s = drafts
    body, group = prepared(s)
    pay(s, body)
    original = Session.get

    def malformed(self, entity, *args, **kwargs):
        row = original(self, entity, *args, **kwargs)
        if entity is CommandReceipt and row is not None:
            response = dict(row.response)
            if failure == "shape":
                response["kind"] = "fictional-unknown"
            if failure == "initial":
                response["initial_allocation"] = None
            return SimpleNamespace(
                hash_version=row.hash_version,
                response_status=201,
                operation_id=uuid4() if failure == "anchor" else row.operation_id,
                response=response,
            )
        return row

    reads = BusinessSettlementReads(s["engine"])
    before = snapshot(s)
    monkeypatch.setattr(Session, "get", malformed)
    for method, identity in ((reads.get_payment, body.id), (reads.get_allocation, group.id)):
        with pytest.raises(LedgerError) as error:
            method(s["owner"], s["ledger"], identity)
        assert (error.value.code, error.value.status) == ("settlement_integrity", 503)
    assert snapshot(s) == before


def test_detail_unknown_identity_is_not_found(drafts):
    s = drafts
    reads = BusinessSettlementReads(s["engine"])
    before = snapshot(s)
    for method in (reads.get_payment, reads.get_allocation):
        with pytest.raises(LedgerError) as error:
            method(s["owner"], s["ledger"], uuid4())
        assert error.value.status == 404
    assert snapshot(s) == before
