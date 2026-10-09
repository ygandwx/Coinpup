"""Draft contracts reject client-calculated values and ambiguous source input."""

from uuid import uuid4

import pytest
from coinpup_api.business.draft_schemas import DraftCreate, DraftLineInput
from pydantic import ValidationError


def line_values():
    return dict(
        id=uuid4(),
        description="Fictional 服务",
        quantity="3.00",
        unit_price="19.99",
        category_id=uuid4(),
        recognition_date="2026-10-09",
    )


@pytest.mark.parametrize(
    "changes",
    [
        dict(quantity=3),
        dict(quantity="0"),
        dict(quantity="1e0"),
        dict(quantity="01"),
        dict(quantity="1\n"),
        dict(quantity="１"),
        dict(quantity="1." + "0" * 19),
        dict(unit_price="-1"),
        dict(unit_price=1.0),
        dict(discount_amount="NaN"),
        dict(tax_rate_percent="-1"),
        dict(description=" \t"),
        dict(description="Fictional\x00"),
        dict(recognition_date=20261009),
        dict(recognition_date="2026-10-09T00:00:00"),
        dict(net_amount="59.97"),
        dict(category_snapshot={"name": "Fictional spoof"}),
        dict(asset_id="USD"),
        dict(ledger_id=str(uuid4())),
        dict(archived=True),
    ],
)
def test_rejects_unsafe_or_server_owned_line_values(changes):
    with pytest.raises(ValidationError):
        DraftLineInput(**(line_values() | changes))


def test_exact_sources_and_bounded_distinct_lines():
    line = DraftLineInput(**(line_values() | dict(unit_price="-0.00", tax_rate_percent="123.4500")))
    assert line.quantity == "3.00" and line.unit_price == "-0.00"
    assert line.tax_rate_percent == "123.4500"
    body = dict(
        id=uuid4(),
        document_kind="invoice",
        party_id=uuid4(),
        asset_id="USD",
        issue_date="2026-10-09",
    )
    assert DraftCreate(**body).lines == []
    assert len(DraftCreate(**body, lines=[line]).lines) == 1
    for lines in ([line, line], [DraftLineInput(**line_values()) for _ in range(201)]):
        with pytest.raises(ValidationError):
            DraftCreate(**body, lines=lines)
    for changes in (dict(issuer_snapshot={}), dict(state="draft"), dict(total_amount="0.00")):
        with pytest.raises(ValidationError):
            DraftCreate(**(body | changes))
