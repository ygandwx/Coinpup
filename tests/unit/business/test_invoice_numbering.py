"""Exact text and finite numbering, not evidence of database allocation or issuance."""

from dataclasses import FrozenInstanceError

import pytest
from coinpup_api.business.invoice_numbering import (
    MAX_SEQUENCE,
    InvoiceNumberingError,
    InvoiceNumberingPolicy,
    candidate_number,
)
from pydantic import ValidationError


@pytest.mark.parametrize(
    "prefix,width,sequence,number,next_sequence",
    [
        ("", 1, "1", "1", "2"),
        ("INV-", 6, "1", "INV-000001", "2"),
        ("虚构-", 6, "99", "虚构-000099", "100"),
        ("INV-", 2, "999", "INV-999", "1000"),
        ("2026/", 1, "9007199254740993", "2026/9007199254740993", "9007199254740994"),
        ("INV-", 20, str(MAX_SEQUENCE), "INV-09223372036854775807", None),
        ("%Y/{year}/", 2, "2", "%Y/{year}/02", "3"),
    ],
)
def test_exact_number_candidate(prefix, width, sequence, number, next_sequence):
    policy = InvoiceNumberingPolicy(prefix=prefix, minimum_digits=width)
    before = policy.model_dump()
    result = candidate_number(policy, sequence)
    assert (result.sequence, result.number, result.next_sequence) == (
        sequence,
        number,
        next_sequence,
    )
    assert policy.model_dump() == before
    assert candidate_number(policy, sequence) == result
    with pytest.raises(FrozenInstanceError):
        result.number = "changed"


@pytest.mark.parametrize(
    "sequence",
    [
        "",
        "0",
        "00",
        "01",
        "-1",
        "+1",
        "1.0",
        "1e2",
        " 1",
        "1 ",
        "1\n",
        "１２",
        "١",
        1,
        1.0,
        True,
        None,
        "1" * 10000,
    ],
)
def test_noncanonical_or_nontext_sequence_rejected(sequence):
    policy = InvoiceNumberingPolicy(prefix="INV-", minimum_digits=6)
    with pytest.raises(InvoiceNumberingError) as error:
        candidate_number(policy, sequence)
    assert error.value.code == "invalid_invoice_sequence"
    assert str(error.value) == "invalid_invoice_sequence"


def test_exhaustion_never_wraps_or_reuses_one():
    policy = InvoiceNumberingPolicy(prefix="INV-", minimum_digits=1)
    penultimate = candidate_number(policy, str(MAX_SEQUENCE - 1))
    assert penultimate.next_sequence == str(MAX_SEQUENCE)
    assert candidate_number(policy, penultimate.next_sequence).next_sequence is None
    with pytest.raises(InvoiceNumberingError, match="invoice_sequence_exhausted"):
        candidate_number(policy, str(MAX_SEQUENCE + 1))


@pytest.mark.parametrize(
    "prefix",
    [" INV", "INV ", "x\n", "x\x00", "x\t", "x\u202e", "x\u200b", "x\u00a0y", "x" * 41, 12, None],
)
def test_prefix_cannot_hide_controls_or_be_coerced(prefix):
    with pytest.raises(ValidationError):
        InvoiceNumberingPolicy(prefix=prefix, minimum_digits=6)


@pytest.mark.parametrize("width", [0, -1, 21, "6", 6.0, True, None])
def test_width_is_a_bounded_strict_integer(width):
    with pytest.raises(ValidationError):
        InvoiceNumberingPolicy(prefix="INV-", minimum_digits=width)


def test_policy_has_no_dynamic_template_or_counter_mutation_input():
    with pytest.raises(ValidationError):
        InvoiceNumberingPolicy(prefix="INV-", minimum_digits=6, next_sequence="1")
    policy = InvoiceNumberingPolicy(prefix="INV-", minimum_digits=6)
    with pytest.raises(ValidationError):
        policy.prefix = "NEW-"
    with pytest.raises(ValidationError):
        candidate_number(policy.model_copy(update={"minimum_digits": True}), "1")
    with pytest.raises(InvoiceNumberingError, match="invalid_invoice_numbering_policy"):
        candidate_number({"prefix": "INV-", "minimum_digits": 6}, "1")


def test_configuration_changes_format_only_not_the_supplied_sequence():
    first = candidate_number(InvoiceNumberingPolicy(prefix="OLD-", minimum_digits=3), "999")
    replacement = candidate_number(
        InvoiceNumberingPolicy(prefix="NEW-", minimum_digits=1), first.next_sequence
    )
    assert first.number == "OLD-999"
    assert replacement.number == "NEW-1000"
    # Equal candidates on a repeat call are not an idempotency/uniqueness guarantee;
    # only the future transaction can reserve one number and retain its old receipt.
    assert candidate_number(InvoiceNumberingPolicy(prefix="OLD-", minimum_digits=3), "999") == first
