"""Fictional confirmed invoices and bills retain source input, order and exact line prices."""

from decimal import localcontext
from uuid import uuid4

import pytest
from coinpup_api.business.document_revision_schemas import (
    ConfirmedDocumentCancellation,
    ConfirmedDocumentCorrection,
)
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.errors import MoneyError
from pydantic import ValidationError


def payload(count=1, kind="invoice"):
    party, category, project = uuid4(), uuid4(), uuid4()
    return {
        "expected_version": 8,
        "reason": " Fictional corrected description 中文 ",
        "replacement": {
            "document_kind": kind,
            "party_id": party,
            "asset_id": "USD",
            "issue_date": "2026-10-10",
            "due_date": "2026-11-10",
            "lines": [
                dict(
                    id=uuid4(),
                    description="Fictional 经营行",
                    quantity="3",
                    unit_price="19.99",
                    discount_amount="9.97",
                    tax_rate_percent="8.25",
                    category_id=category,
                    project_id=project,
                    recognition_date="2026-10-01",
                )
                for _ in range(count)
            ],
            "reviewed_references": [
                dict(kind=kind, id=i, version=v)
                for kind, i, v in [
                    ("party", party, 11),
                    ("category", category, 7),
                    ("project", project, 3),
                ]
            ],
        },
    }


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_full_ordered_replacement_prices_each_rounded_line_and_zero_without_draft_writes(kind):
    raw = payload(200, kind)
    raw["replacement"]["lines"][-1].update(quantity="1", unit_price="0", discount_amount="0")
    cmd = ConfirmedDocumentCorrection.model_validate(raw)
    assert cmd.expected_version == 8 and cmd.reason == raw["reason"].strip()
    assert [line.id for line in cmd.replacement.lines] == [
        line["id"] for line in raw["replacement"]["lines"]
    ]
    with localcontext() as ctx:
        ctx.prec = 2
        priced = cmd.replacement.price(kind, ASSETS["USD"])
    assert (
        priced.lines[0].net.minor_units,
        priced.lines[0].tax.minor_units,
        priced.lines[0].total.minor_units,
    ) == (5000, 413, 5413)
    assert priced.lines[-1].total.minor_units == 0
    assert priced.total.minor_units == 199 * 5413
    assert (
        cmd.replacement.lines[0].unit_price == "19.99"
        and cmd.replacement.lines[0].discount_amount == "9.97"
    )
    assert raw["reason"].startswith(" ")


@pytest.mark.parametrize("bad", [1, 0, False, 2.0, "2", 2147483647])
def test_confirmed_version_is_the_document_version_after_confirmation(bad):
    with pytest.raises(ValidationError):
        ConfirmedDocumentCancellation.model_validate(
            dict(expected_version=bad, reason="Fictional cancel")
        )
    for ok in [2, 237, 2147483646]:
        assert (
            ConfirmedDocumentCancellation(
                expected_version=ok, reason="Fictional cancel"
            ).expected_version
            == ok
        )


@pytest.mark.parametrize(
    "change",
    [
        "empty",
        "201",
        "duplicate-line",
        "duplicate-pin",
        "foreign-pin",
        "wrong-kind-pin",
        "bad-version",
        "computed-total",
        "extra-document",
        "float-price",
    ],
)
def test_unreviewable_submitted_identity_or_amount_is_rejected(change):
    cmd = payload(201 if change == "201" else 1)
    r = cmd["replacement"]
    if change == "empty":
        r["lines"] = []
    elif change == "duplicate-line":
        r["lines"] *= 2
    elif change == "duplicate-pin":
        r["reviewed_references"].append(r["reviewed_references"][0])
    elif change == "foreign-pin":
        r["reviewed_references"][0]["id"] = uuid4()
    elif change == "wrong-kind-pin":
        r["reviewed_references"][0]["kind"] = "category"
    elif change == "bad-version":
        r["reviewed_references"][0]["version"] = True
    elif change == "computed-total":
        r["lines"][0]["total_amount"] = "1.00"
    elif change == "extra-document":
        r["id"] = uuid4()
    elif change == "float-price":
        r["lines"][0]["unit_price"] = 19.99
    with pytest.raises(ValidationError):
        ConfirmedDocumentCorrection.model_validate(cmd)


def test_no_zero_total_excess_precision_or_wrong_asset_and_kind():
    cmd = payload()
    cmd["replacement"]["lines"][0].update(unit_price="0", discount_amount="0")
    zero = ConfirmedDocumentCorrection.model_validate(cmd).replacement
    with pytest.raises(MoneyError, match="positive"):
        zero.price("invoice", ASSETS["USD"])
    cmd["replacement"]["lines"][0]["unit_price"] = "19.999"
    row = ConfirmedDocumentCorrection.model_validate(cmd).replacement
    for kind, asset, code in [
        ("invoice", "USD", "amount_precision"),
        ("invoice", "EUR", "document_revision_scope"),
        ("bill", "USD", "document_revision_scope"),
    ]:
        with pytest.raises(MoneyError) as e:
            row.price(kind, ASSETS[asset])
        assert e.value.code == code
