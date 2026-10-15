"""Route-bound return intents preserve amount spelling and ordered original members."""

import json
from uuid import uuid4

import pytest
from coinpup_api.business.allocation_return_schemas import BusinessAllocationReturnCreate
from coinpup_api.ledger.idempotency import _v2_hash
from coinpup_api.ledger.service import LedgerError


def body():
    return BusinessAllocationReturnCreate(
        id=uuid4(),
        payment_id=uuid4(),
        return_date="2026-10-15",
        reason="Fictional 退回",
        lines=[
            dict(
                source_allocation_line_id=uuid4(),
                expected_version=1,
                payment_amount="20",
                document_amount="20",
            )
            for _ in range(2)
        ],
    )


def test_return_hash_is_route_bound_and_json_formatting_does_not_create_new_intent():
    payload, ledger = body(), uuid4()
    raw = json.dumps(payload.model_dump(mode="json"), indent=2, ensure_ascii=False).encode()
    digest = _v2_hash("business_allocation_return", ledger, payload, raw)
    assert digest == _v2_hash("business_allocation_return", ledger, payload, None)
    assert digest != _v2_hash("business_allocation", ledger, payload, raw)
    assert digest != _v2_hash("business_payment", ledger, payload, raw)
    assert digest != _v2_hash("business_allocation_return", uuid4(), payload, raw)
    changed = payload.model_dump(mode="json")
    changed["lines"][0]["payment_amount"] = "20.00"
    assert digest != _v2_hash("business_allocation_return", ledger, payload, json.dumps(changed))
    reversed_body = payload.model_copy(update={"lines": list(reversed(payload.lines))})
    assert digest != _v2_hash("business_allocation_return", ledger, reversed_body, None)


@pytest.mark.parametrize("raw", [b"[]", b"{", b'{"value":NaN}'])
def test_return_hash_rejects_invalid_json_without_accepting_an_ambiguous_intent(raw):
    with pytest.raises(LedgerError) as error:
        _v2_hash("business_allocation_return", uuid4(), body(), raw)
    assert error.value.code == "invalid_request"
