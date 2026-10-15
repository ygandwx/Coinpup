"""Raw refund intent and immutable receipt integrity cannot be inferred from current balances."""

import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from coinpup_api.business.refund_receipts import replay_refund
from coinpup_api.business.refund_schemas import BusinessRefundResponse
from coinpup_api.ledger.idempotency import _v2_hash
from coinpup_api.ledger.service import LedgerError

from tests.unit.business.test_refund_plan import fixture


def test_route_raw_amount_spelling_and_return_order_are_part_of_intent():
    ledger, _, _, body, _ = fixture()
    body = body.model_copy(update={"allocation_return_ids": [uuid4(), uuid4()]})
    raw = json.dumps(body.model_dump(mode="json"), ensure_ascii=False, indent=2)
    digest = _v2_hash("business_refund", ledger, body, raw)
    assert digest == _v2_hash("business_refund", ledger, body, json.dumps(json.loads(raw)))
    assert digest != _v2_hash("business_payment", ledger, body, raw)
    assert digest != _v2_hash("business_refund", uuid4(), body, raw)
    changed = json.loads(raw) | {"amount": "20.00"}
    assert digest != _v2_hash("business_refund", ledger, body, json.dumps(changed))
    changed = json.loads(raw) | {
        "allocation_return_ids": list(reversed(json.loads(raw)["allocation_return_ids"]))
    }
    assert digest != _v2_hash("business_refund", ledger, body, json.dumps(changed))


@pytest.mark.parametrize("fault", [None, "body", "receipt", "anchor", "status", "hash", "version"])
def test_replay_requires_exact_saved_header_response_and_operation(fault):
    ledger, payment, _, body, _ = fixture()
    saved = SimpleNamespace(
        **(
            body.model_dump()
            | dict(
                source_version=body.expected_version,
                amount=Decimal(body.amount),
                payment_amount=Decimal(body.payment_amount),
            )
        )
    )
    response = BusinessRefundResponse(
        kind="business_refund",
        id=body.id,
        ledger_id=ledger,
        payment_id=payment.id,
        operation_id=uuid4(),
        journal_id=uuid4(),
        account_id=body.account_id,
        asset_id=body.asset_id,
        amount=body.amount,
        payment_asset_id=payment.amount.asset.asset_id,
        payment_amount=body.payment_amount,
        transaction_date=body.transaction_date,
        allocation_return_ids=tuple(body.allocation_return_ids),
        fees=(),
    ).model_dump(mode="json")
    receipt = SimpleNamespace(
        hash_version=2,
        request_hash="fictional-digest",
        key="fictional-key",
        response_status=201,
        operation_id=response["operation_id"],
        response=response,
    )
    # ORM UUID matches the parsed receipt UUID; corrupted strings must not silently coerce.
    from uuid import UUID

    receipt.operation_id = UUID(receipt.operation_id)
    if fault == "body":
        saved.reason = "different fictional reason"
    elif fault == "receipt":
        receipt.response = response | {"amount": "1"}
    elif fault == "anchor":
        receipt.operation_id = uuid4()
    elif fault == "status":
        receipt.response_status = 200
    elif fault == "hash":
        receipt.request_hash = "other"
    elif fault == "version":
        receipt.hash_version = 1
    values = iter((saved, response))
    session = SimpleNamespace(scalar=lambda _: next(values))
    if fault is None:
        assert (
            replay_refund(session, receipt, ledger, body, "fictional-digest").model_dump(
                mode="json"
            )
            == response
        )
    else:
        with pytest.raises(LedgerError) as error:
            replay_refund(session, receipt, ledger, body, "fictional-digest")
        assert error.value.code == (
            "idempotency_conflict" if fault in {"hash", "version"} else "refund_integrity"
        )
