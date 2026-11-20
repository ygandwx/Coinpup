"""Strict reviewed identities; financial facts and assigned numbers remain server owned."""

from uuid import uuid4

import pytest
from coinpup_api.business.invoice_issuance_schemas import (
    InvoiceIssuanceCreate,
    InvoiceProfileArchive,
    InvoiceProfileCreate,
    InvoiceProfileReplacement,
)
from pydantic import ValidationError


def profile():
    return dict(
        id=str(uuid4()),
        name="Fictional invoice configuration",
        numbering={"prefix": "虚构-", "minimum_digits": 6},
        issuer_extra_text="Fictional 公司\nFictional address",
        payment_instructions="Fictional account only\nPlease quote the Invoice number.",
        footer="Fictional commercial Invoice, not a tax invoice.",
        logo_file_id=str(uuid4()),
        logo_file_version=2,
    )


def intent():
    return dict(
        id=str(uuid4()),
        document_id=str(uuid4()),
        expected_document_version=9,
        configuration_id=str(uuid4()),
        expected_configuration_version=3,
        previous_issuance_id=None,
    )


def test_profile_roundtrip_retains_reviewed_text_and_complete_number_policy():
    body = profile()
    result = InvoiceProfileCreate.model_validate(body)
    assert result.model_dump(mode="json") == body
    replacement = body | {"expected_version": 7}
    del replacement["id"]
    assert (
        InvoiceProfileReplacement.model_validate(replacement).model_dump(mode="json") == replacement
    )
    no_logo = body | dict(logo_file_id=None, logo_file_version=None)
    assert InvoiceProfileCreate.model_validate(no_logo).model_dump(mode="json") == no_logo


@pytest.mark.parametrize(
    "change",
    [
        {"logo_file_id": None},
        {"logo_file_version": None},
        {"logo_file_version": True},
        {"logo_file_version": "2"},
        {"logo_file_version": 0},
        {"logo_file_id": "not-a-uuid"},
        {"numbering": {"prefix": "INV-", "minimum_digits": "6"}},
        {"numbering": {"prefix": "INV-", "minimum_digits": 6, "next_sequence": "1"}},
        {"next_sequence": "1"},
        {"number": "INV-000001"},
        {"owner_id": str(uuid4())},
        {"ledger_id": str(uuid4())},
        {"template_url": "https://invalid.example/remote"},
        {"logo_url": "https://invalid.example/logo.png"},
    ],
)
def test_profile_rejects_unreviewed_logo_or_server_owned_inputs(change):
    with pytest.raises(ValidationError):
        InvoiceProfileCreate.model_validate(profile() | change)


@pytest.mark.parametrize("field", ["issuer_extra_text", "payment_instructions", "footer"])
@pytest.mark.parametrize("value", ["NUL\x00", "x" * 2001, 123, True])
def test_display_text_is_bounded_and_cannot_be_coerced(field, value):
    with pytest.raises(ValidationError):
        InvoiceProfileCreate.model_validate(profile() | {field: value})


def test_issuance_requires_explicit_original_or_predecessor_and_all_reviewed_versions():
    body = intent()
    assert InvoiceIssuanceCreate.model_validate(body).model_dump(mode="json") == body
    later = body | {"previous_issuance_id": str(uuid4())}
    assert InvoiceIssuanceCreate.model_validate(later).model_dump(mode="json") == later
    for field in body:
        missing = body.copy()
        del missing[field]
        with pytest.raises(ValidationError):
            InvoiceIssuanceCreate.model_validate(missing)
    with pytest.raises(ValidationError):
        InvoiceIssuanceCreate.model_validate(body | {"previous_issuance_id": body["id"]})


@pytest.mark.parametrize("field", ["expected_document_version", "expected_configuration_version"])
@pytest.mark.parametrize("value", [None, 0, -1, True, "9", 9.0])
def test_issuance_version_is_explicit_positive_integer(field, value):
    with pytest.raises(ValidationError):
        InvoiceIssuanceCreate.model_validate(intent() | {field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("number", "INV-000001"),
        ("sequence", "1"),
        ("total_amount", "100.00"),
        ("net_amount", "90.00"),
        ("tax_amount", "10.00"),
        ("lines", []),
        ("ledger_id", str(uuid4())),
        ("owner_id", str(uuid4())),
        ("pdf_zh_file_id", str(uuid4())),
        ("pdf_en_file_id", str(uuid4())),
        ("locale", "en"),
        ("auto_send", True),
    ],
)
def test_issuance_cannot_supply_amounts_numbers_files_or_send_mail(field, value):
    with pytest.raises(ValidationError):
        InvoiceIssuanceCreate.model_validate(intent() | {field: value})


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_archive_is_explicit_boolean(value):
    with pytest.raises(ValidationError):
        InvoiceProfileArchive(expected_version=1, archived=value)


def test_archive_does_not_mutate_number_policy_or_issue_a_new_invoice():
    assert InvoiceProfileArchive(expected_version=2, archived=True).model_dump() == {
        "expected_version": 2,
        "archived": True,
    }
    with pytest.raises(ValidationError):
        InvoiceProfileArchive(expected_version=2, archived=False, next_sequence="1")
