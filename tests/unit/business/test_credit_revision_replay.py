"""Real exact quantities and full original intentions, without a fabricated posting service."""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest
from coinpup_api.business.credit_revision_receipts import validate_revision_intent
from coinpup_api.business.credit_revision_schemas import (
    CreditCancellationCreate,
    CreditCorrectionCreate,
    CreditRevisionResponse,
)
from coinpup_api.ledger.assets import ASSETS, AssetDefinition
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.service import LedgerError

from tests.unit.business.test_credit_revision_contract import member, receipt


def intent(response, asset=ASSETS["USD"]):
    data = dict(expected_version=response.previous_version, reason=response.reason)
    if response.action == "cancel":
        return CreditCancellationCreate(**data)
    return CreditCorrectionCreate(
        **data,
        replacement=dict(
            credit_date=response.credit_date,
            reason=response.replacement_reason,
            lines=[
                dict(
                    source_line_id=item.source_line_id,
                    recognition_date=item.recognition_date,
                    **{
                        field: Amount.from_decimal(Decimal(getattr(item, field)), asset).to_string()
                        for field in ("net_amount", "tax_amount")
                    },
                )
                for item in response.members
                if item.action != "cancel"
            ],
        ),
    )


def mixed():
    return CreditRevisionResponse.model_validate(
        receipt()
        | {"members": [member(), member("create", current=2), member("cancel", previous=2)]}
    )


@pytest.mark.parametrize("action", ["correct", "cancel"])
def test_full_frozen_intent_keeps_padded_strings_after_asset_is_disabled(action):
    response = (
        mixed() if action == "correct" else CreditRevisionResponse.model_validate(receipt(action))
    )
    before = response.model_dump(mode="json")
    body = intent(response)
    with localcontext() as context:
        context.prec = 2
        assert (
            validate_revision_intent(response, action, body, replace(ASSETS["USD"], enabled=False))
            is response
        )
    assert response.model_dump(mode="json") == before


@pytest.mark.parametrize(
    "fault",
    [
        "source",
        "date",
        "net",
        "tax",
        "order",
        "missing",
        "extra",
        "credit_date",
        "fact_reason",
        "reason",
        "version",
        "action",
    ],
)
def test_valid_but_different_intent_cannot_claim_an_old_group(fault):
    response = mixed()
    data = intent(response).model_dump(mode="json")
    replacement = data["replacement"]
    line = replacement["lines"][0]
    if fault == "source":
        line["source_line_id"] = str(uuid4())
    elif fault == "date":
        line["recognition_date"] = "2026-10-22"
    elif fault in ("net", "tax"):
        line[fault + "_amount"] = "1.00"
    elif fault == "order":
        replacement["lines"].reverse()
    elif fault == "missing":
        replacement["lines"].pop()
    elif fault == "extra":
        replacement["lines"].append(line | {"source_line_id": str(uuid4())})
    elif fault == "credit_date":
        replacement["credit_date"] = "2026-11-04"
    elif fault == "fact_reason":
        replacement["reason"] = "Fictional other replacement"
    elif fault == "reason":
        data["reason"] = "Fictional other revision"
    elif fault == "version":
        data["expected_version"] -= 1
    payload = CreditCorrectionCreate.model_validate(data)
    with pytest.raises(LedgerError) as failure:
        validate_revision_intent(
            response, "cancel" if fault == "action" else "correct", payload, ASSETS["USD"]
        )
    assert (failure.value.code, failure.value.status) == ("credit_revision_integrity", 503)


def test_full_custom_asset_identity_and_smallest_unit_do_not_use_decimal_context():
    asset = AssetDefinition("USDT", "token", 18, "fictional", "FictionalToken", enabled=False)
    data = receipt() | {"asset_id": asset.asset_id}
    for prefix in ("previous_", ""):
        data["members"][0].update(
            **{
                prefix + "net_amount": "99999999999999999999.999999999999999998",
                prefix + "tax_amount": "0.000000000000000001",
                prefix + "total_amount": "99999999999999999999.999999999999999999",
            }
        )
    response = CreditRevisionResponse.model_validate(data)
    body = intent(response, asset)
    with localcontext() as context:
        context.prec = 2
        assert validate_revision_intent(response, "correct", body, asset) is response
    with pytest.raises(LedgerError):
        validate_revision_intent(response, "correct", body, replace(asset, token_reference="Other"))


@pytest.mark.parametrize("fault", ["precision", "sum", "asset"])
def test_decoding_uses_actual_asset_precision_and_components_on_both_sides(fault):
    response = mixed()
    body = intent(response)
    data = deepcopy(response.model_dump(mode="json"))
    data["members"][0]["previous_net_amount"] = "9.001" if fault == "precision" else "8.00"
    altered = CreditRevisionResponse.model_validate(data) if fault != "asset" else response
    with pytest.raises(LedgerError) as failure:
        validate_revision_intent(
            altered, "correct", body, ASSETS["EUR"] if fault == "asset" else ASSETS["USD"]
        )
    assert failure.value.code == "credit_revision_integrity"
