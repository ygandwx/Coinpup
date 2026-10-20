"""Complete saved credit facts, not current availability, prove an original success."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from coinpup_api.business.credit_hash import credit_hash_v2
from coinpup_api.business.credit_receipts import replay_credit
from coinpup_api.business.credit_schemas import BusinessCreditCreate, BusinessCreditResponse
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.service import LedgerError

from tests.unit.business.test_credit_contract import response


def scenario():
    saved = BusinessCreditResponse.model_validate(response())
    request = BusinessCreditCreate(
        id=saved.id,
        document_id=saved.document_id,
        expected_version=saved.source_version,
        credit_date=saved.credit_date,
        reason="Fictional original credit",
        lines=[
            dict(
                source_line_id=row.source_line_id,
                net_amount=row.net_amount,
                tax_amount=row.tax_amount,
                recognition_date=row.recognition_date,
            )
            for row in saved.postings
        ],
    )
    header = SimpleNamespace(
        document_id=saved.document_id,
        source_version=saved.source_version,
        credit_date=saved.credit_date,
        reason=request.reason,
        id=saved.id,
    )
    data = saved.model_dump(mode="json")
    session = SimpleNamespace(
        get=lambda *args: replace(ASSETS["USD"], enabled=False),
        scalar=lambda statement: header if "FROM business_credits" in str(statement) else data,
    )
    digest = credit_hash_v2(saved.ledger_id, request)
    receipt = SimpleNamespace(
        key="fictional-credit",
        hash_version=2,
        request_hash=digest,
        response_status=201,
        operation_id=saved.postings[0].operation_id,
        response=deepcopy(data),
    )
    return saved, request, session, receipt, digest


def test_disabled_historical_asset_keeps_original_padded_receipt_unchanged():
    saved, request, session, receipt, digest = scenario()
    assert replay_credit(session, receipt, saved.ledger_id, request, digest) == saved
    assert receipt.response["postings"][0]["total_amount"] == "20.000000000000000000"


@pytest.mark.parametrize(
    "fault", ["hash", "legacy", "status", "anchor", "missing", "amount", "order"]
)
def test_corrupt_or_different_intentions_cannot_be_mistaken_for_a_saved_credit(fault):
    saved, request, session, receipt, digest = scenario()
    if fault == "hash":
        digest = "0" * 64
    elif fault == "legacy":
        receipt.hash_version = 1
    elif fault == "status":
        receipt.response_status = 200
    elif fault == "anchor":
        receipt.operation_id = saved.postings[1].operation_id
    elif fault == "missing":
        session.scalar = lambda statement: None
    elif fault == "amount":
        receipt.response["postings"][0]["net_amount"] = "17.22"
    else:
        receipt.response["postings"].reverse()
    with pytest.raises(LedgerError) as failure:
        replay_credit(session, receipt, saved.ledger_id, request, digest)
    assert failure.value.code == (
        "idempotency_conflict" if fault in {"hash", "legacy"} else "credit_integrity"
    )
