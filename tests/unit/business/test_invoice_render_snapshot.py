"""Frozen company, document, Logo and number identities for explicitly fictional Invoices."""

import hashlib
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from coinpup_api.business.invoice_profiles import InvoiceProfileResponse
from coinpup_api.business.invoice_render_snapshot import (
    INVOICE_TEMPLATE_VERSION,
    freeze_invoice_render_input,
)
from coinpup_api.files.schemas import FileResponse
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.errors import MoneyError

from tests.unit.business.test_invoice_document_snapshot import source


def inputs(count=3, with_logo=False):
    facts, command = source([1] + [0] * (count - 1))
    stamp = datetime(2026, 11, 13, tzinfo=UTC)
    logo = (
        FileResponse(
            id=uuid4(),
            ledger_id=facts.ledger_id,
            created_by=uuid4(),
            sha256="a" * 64,
            byte_size=123,
            detected_media_type="image/png",
            original_filename="Fictional.png",
            title="Fictional 示例 Logo",
            archived=False,
            version=1,
            created_at=stamp,
            updated_at=stamp,
        )
        if with_logo
        else None
    )
    profile = InvoiceProfileResponse(
        id=command.configuration_id,
        ledger_id=facts.ledger_id,
        archived=False,
        version=command.expected_configuration_version,
        created_at=stamp,
        updated_at=stamp,
        name="Fictional 虚构公司",
        numbering={"prefix": "示例-INV-", "minimum_digits": 5},
        issuer_extra_text="Fictional 地址\n纯文本 <script>0</script>",
        payment_instructions="Fictional 收款说明",
        footer="Fictional footer",
        logo_file_id=logo.id if logo else None,
        logo_file_version=1 if logo else None,
    )
    return facts, command, profile, logo


def freeze(facts, command, profile, logo, sequence="1", **kwargs):
    return freeze_invoice_render_input(
        facts,
        ASSETS["USD"],
        profile,
        ledger_id=facts.ledger_id,
        command=command,
        sequence=sequence,
        logo=logo,
        **kwargs,
    )


@pytest.mark.parametrize("count", [1, 200])
@pytest.mark.parametrize("with_logo", [False, True])
def test_complete_render_input_is_immutable_and_deterministic(count, with_logo):
    facts, command, profile, logo = args = inputs(count, with_logo)
    before = facts.model_dump(mode="json"), profile.model_dump(mode="json")
    frozen = freeze(*args)
    decoded = frozen.decode()
    assert decoded["template_version"] == INVOICE_TEMPLATE_VERSION
    assert decoded["schema_version"] == 1
    assert decoded["intent"] == command.model_dump(mode="json")
    assert decoded["number"] == {"sequence": "1", "number": "示例-INV-00001"}
    assert decoded["document"]["current"] == before[0]
    assert len(decoded["document"]["current"]["document"]["lines"]) == count
    assert decoded["profile"] == before[1]
    if with_logo:
        assert decoded["logo"] == dict(
            file_id=str(logo.id),
            ledger_id=str(facts.ledger_id),
            reviewed_version=1,
            sha256="a" * 64,
            byte_size=123,
            media_type="image/png",
        )
    else:
        assert decoded["logo"] is None
    assert freeze(*args) == frozen
    assert frozen.sha256 == hashlib.sha256(frozen.content).hexdigest()
    assert "虚构".encode() in frozen.content
    decoded["profile"]["name"] = "Altered preview"
    decoded["document"]["current"]["document"]["lines"].clear()
    assert frozen.decode()["profile"] == before[1]
    assert frozen.decode()["document"]["current"] == before[0]
    with pytest.raises(FrozenInstanceError):
        frozen.content = b"replacement"


@pytest.mark.parametrize("sequence", ["9007199254740993", str(2**63 - 1)])
def test_sequence_stays_exact_and_is_never_encoded_as_a_json_number(sequence):
    frozen = freeze(*inputs(), sequence=sequence)
    assert frozen.decode()["number"] == {"sequence": sequence, "number": "示例-INV-" + sequence}


@pytest.mark.parametrize("sequence", [0, 1.0, True, "0", "01", "-1", str(2**63), "1e3"])
def test_invalid_or_exhausted_sequence_never_becomes_a_render_input(sequence):
    with pytest.raises(MoneyError, match="reviewed Invoice inputs"):
        freeze(*inputs(), sequence=sequence)


def test_later_profile_document_or_logo_changes_do_not_rewrite_frozen_evidence():
    facts, command, profile, logo = args = inputs(with_logo=True)
    frozen = freeze(*args)
    # Reviewed file content is immutable. A later title edit does not replace that identity.
    logo.title, logo.version = "Changed fictional title", 9
    assert freeze(*args) == frozen
    profile.payment_instructions = "Changed fictional instructions"
    facts.document.lines[0].description = "Changed fictional line"
    changed = freeze(*args)
    assert changed.sha256 != frozen.sha256
    assert frozen.decode()["profile"]["payment_instructions"] == "Fictional 收款说明"
    assert changed.decode()["logo"] == frozen.decode()["logo"]
    assert "title" not in changed.decode()["logo"]
    assert "original_filename" not in changed.decode()["logo"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", uuid4()),
        ("ledger_id", uuid4()),
        ("version", 2),
        ("version", True),
        ("version", 1.0),
        ("archived", True),
        ("archived", 0),
        ("name", 123),
    ],
)
def test_stale_cross_scope_archived_or_unvalidated_profile_is_rejected(field, value):
    facts, command, profile, logo = inputs()
    broken = profile.model_copy(update={field: value})
    with pytest.raises(MoneyError):
        freeze(facts, command, broken, logo)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", uuid4()),
        ("ledger_id", uuid4()),
        ("version", 0),
        ("version", True),
        ("archived", True),
        ("archived", 0),
        ("sha256", "A" * 64),
        ("sha256", "invalid"),
        ("sha256", b"a" * 64),
        ("byte_size", 0),
        ("byte_size", 2**63),
        ("byte_size", True),
        ("byte_size", 123.0),
        ("detected_media_type", "application/pdf"),
        ("detected_media_type", "image/svg+xml"),
    ],
)
def test_wrong_or_unsafe_logo_identity_is_never_frozen(field, value):
    facts, command, profile, logo = inputs(with_logo=True)
    with pytest.raises(MoneyError):
        freeze(facts, command, profile, logo.model_copy(update={field: value}))


def test_missing_unreviewed_or_half_paired_logo_is_rejected():
    facts, command, profile, logo = inputs(with_logo=True)
    for reviewed, selected in [
        (profile, None),
        (profile.model_copy(update={"logo_file_id": None, "logo_file_version": None}), logo),
        (profile.model_copy(update={"logo_file_version": None}), logo),
    ]:
        with pytest.raises(MoneyError):
            freeze(facts, command, reviewed, selected)


def test_document_integrity_checks_are_applied_before_rendering():
    facts, command, profile, logo = args = inputs(200)
    facts.document.lines.pop()
    with pytest.raises(MoneyError):
        freeze(*args)


def test_bad_copied_intent_and_wrong_types_do_not_bypass_validation():
    facts, command, profile, logo = inputs()
    for intent in [
        command.model_copy(update={"expected_configuration_version": 0}),
        command.model_copy(update={"expected_configuration_version": True}),
        command.model_copy(update={"previous_issuance_id": command.id}),
        command.model_dump(),
    ]:
        with pytest.raises(MoneyError):
            freeze(facts, intent, profile, logo)
    with pytest.raises(MoneyError):
        freeze(facts, command, profile.model_dump(), logo)


def test_previous_issuance_and_full_bounded_unicode_text_are_frozen_verbatim():
    facts, command, profile, logo = inputs(with_logo=True)
    previous = uuid4()
    command = command.model_copy(update={"previous_issuance_id": previous})
    for field in ("issuer_extra_text", "payment_instructions", "footer"):
        setattr(profile, field, "虚构🦊" * 666)
    frozen = freeze(facts, command, profile, logo)
    decoded = frozen.decode()
    assert decoded["intent"]["previous_issuance_id"] == str(previous)
    assert decoded["profile"] == profile.model_dump(mode="json")
    assert decoded["document"]["current"] == facts.model_dump(mode="json")
