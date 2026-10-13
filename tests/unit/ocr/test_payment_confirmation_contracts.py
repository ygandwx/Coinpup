"""Fictional payment envelopes preserve full initial membership and distinct identities."""

from uuid import uuid4

import pytest
from coinpup_api.ocr.payment_confirmation_contracts import (
    LinkPaymentEntry,
    PaymentLinkConfirmationResponse,
)
from pydantic import ValidationError


def envelope(count=0):
    ledger, payment = uuid4(), uuid4()
    initial = None
    if count:
        initial = dict(
            kind="business_allocation",
            id=str(uuid4()),
            ledger_id=str(ledger),
            payment_id=str(payment),
            postings=[
                dict(
                    line_no=n,
                    document_id=str(uuid4()),
                    document_line_id=str(uuid4()),
                    operation_id=str(uuid4()),
                    journal_id=str(uuid4()),
                )
                for n in range(1, count + 1)
            ],
        )
    return dict(
        intent_id=str(uuid4()),
        draft_id=str(uuid4()),
        ledger_id=str(uuid4()),
        draft_version=2,
        action="link_payment",
        target_ledger_id=str(ledger),
        target_file_id=str(uuid4()),
        operation_version=1,
        payment=dict(
            kind="business_payment",
            id=str(payment),
            ledger_id=str(ledger),
            operation_id=str(uuid4()),
            journal_id=str(uuid4()),
            initial_allocation=initial,
        ),
    )


@pytest.mark.parametrize("count", [0, 1, 200])
def test_original_payment_and_complete_initial_receipt_round_trip(count):
    raw = envelope(count)
    receipt = PaymentLinkConfirmationResponse.model_validate(raw)
    assert receipt.model_dump(mode="json") == raw
    assert "operation" not in receipt.model_dump()
    assert receipt.payment.id != receipt.payment.operation_id


@pytest.mark.parametrize(
    "damage", ["extra", "missing", "ledger", "unordered", "duplicate", "parent"]
)
def test_partial_or_foreign_original_membership_is_rejected(damage):
    raw = envelope(200)
    if damage == "extra":
        raw["operation"] = {}
    elif damage == "missing":
        del raw["payment"]["initial_allocation"]
    elif damage == "ledger":
        raw["target_ledger_id"] = str(uuid4())
    else:
        group = raw["payment"]["initial_allocation"]
        if damage == "unordered":
            group["postings"].reverse()
        elif damage == "duplicate":
            group["postings"][-1]["operation_id"] = group["postings"][0]["operation_id"]
        else:
            group["payment_id"] = str(uuid4())
    with pytest.raises(ValidationError):
        PaymentLinkConfirmationResponse.model_validate(raw)


@pytest.mark.parametrize("version", [0, -1, True, "1", 2147483648])
def test_entry_requires_explicit_payment_identity_and_strict_version(version):
    with pytest.raises(ValidationError):
        LinkPaymentEntry(kind="link_payment", payment_id=uuid4(), expected_version=version)


def test_payment_entry_does_not_accept_legacy_operation_or_creation_commands():
    with pytest.raises(ValidationError):
        LinkPaymentEntry(kind="link_payment", payment_id=uuid4(), expected_version=1, command={})
    with pytest.raises(ValidationError):
        LinkPaymentEntry(kind="link_payment", operation_id=uuid4(), expected_version=1)
