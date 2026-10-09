"""Shared explicitly fictional vectors keep browser and server pricing aligned."""

import json
from pathlib import Path

import pytest
from coinpup_api.business.pricing import PriceInput, price_document
from coinpup_api.ledger.assets import fiat_asset
from coinpup_api.ledger.errors import MoneyError

CASES = json.loads(
    (Path(__file__).parents[2] / "fixtures/business-pricing.json").read_text(encoding="utf-8")
)["cases"]


def serialized(price):
    return {name + "_amount": getattr(price, name).to_string() for name in ("net", "tax", "total")}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_shared_browser_and_server_examples(case):
    asset = fiat_asset(code="FIC", scale=case["scale"])
    source = [PriceInput(**line) for line in case["lines"]]
    if "error" in case:
        with pytest.raises(MoneyError) as error:
            price_document(source, asset)
        assert error.value.code == case["error"]
    else:
        result = price_document(source, asset)
        assert (
            dict(lines=[serialized(line) for line in result.lines], **serialized(result))
            == case["expected"]
        )
