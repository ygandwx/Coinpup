"""Saved bytes are verified against their owned digest and never rebuilt from current records."""

import hashlib
import json
from uuid import uuid4

import pytest
from coinpup_api.business.invoice_render_snapshot import MAX_RENDER_SNAPSHOT_BYTES
from coinpup_api.business.invoice_snapshot_reads import read_invoice_render_input
from coinpup_api.ledger.errors import MoneyError

from tests.unit.business.test_invoice_render_snapshot import freeze, inputs


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def read(content, digest=None):
    return read_invoice_render_input(
        content, expected_sha256=hashlib.sha256(content).hexdigest() if digest is None else digest
    )


@pytest.mark.parametrize("count", [1, 200])
@pytest.mark.parametrize("logo", [False, True])
def test_owned_saved_input_round_trips_exactly_with_all_zero_rows_and_both_logo_forms(count, logo):
    frozen = freeze(*inputs(count, logo), sequence="9007199254740993")
    loaded = read(frozen.content, frozen.sha256)
    assert loaded == frozen and loaded.sha256 == frozen.sha256
    assert len(loaded.decode()["document"]["current"]["document"]["lines"]) == count
    assert loaded.decode()["number"]["sequence"] == "9007199254740993"
    decoded = loaded.decode()
    decoded["document"]["current"]["document"]["lines"].clear()
    decoded["profile"]["name"] = "Changed preview"
    assert loaded.content == frozen.content


def test_later_profile_document_and_logo_mutation_does_not_change_pending_saved_input():
    facts, intent, profile, logo = args = inputs(with_logo=True)
    frozen = freeze(*args)
    profile.archived, profile.version, profile.footer = True, 9, "New fictional footer"
    logo.archived, logo.version, logo.title = True, 10, "New fictional title"
    facts.document.lines[0].description = "New fictional description"
    intent.expected_configuration_version = 9
    loaded = read(frozen.content, frozen.sha256)
    assert loaded == frozen
    assert loaded.decode()["profile"]["version"] == 1
    assert loaded.decode()["logo"]["reviewed_version"] == 1


@pytest.mark.parametrize("digest", [None, 1, "", "f" * 64, "A" * 64, "a" * 63])
def test_original_owned_digest_is_required(digest):
    frozen = freeze(*inputs())
    with pytest.raises(MoneyError) as caught:
        read_invoice_render_input(frozen.content, expected_sha256=digest)
    assert caught.value.code == "invoice_render_integrity"
    assert "Fictional" not in str(caught.value)


def test_a_valid_but_changed_snapshot_cannot_be_read_against_the_original_digest():
    frozen = freeze(*inputs())
    altered = frozen.decode()
    altered["profile"]["footer"] = "Other fictional immutable input"
    with pytest.raises(MoneyError):
        read(canonical(altered), frozen.sha256)


@pytest.mark.parametrize("content", [b"", b"{}", b"[]", b"null", b"\xff", b"{", b"[[[]]]"])
def test_malformed_or_incomplete_input_returns_a_stable_domain_error(content):
    with pytest.raises(MoneyError):
        read(content)


def test_wrong_byte_container_and_oversized_input_are_rejected_before_decoding():
    frozen = freeze(*inputs())
    for content in (
        frozen.content.decode(),
        bytearray(frozen.content),
        b"x" * (MAX_RENDER_SNAPSHOT_BYTES + 1),
    ):
        with pytest.raises(MoneyError):
            read_invoice_render_input(content, expected_sha256=frozen.sha256)


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema_version",), 2),
        (("template_version",), "unreviewed-v2"),
        (("number", "number"), "Other-1"),
        (("number", "sequence"), "01"),
        (("number", "sequence"), 1),
        (("number", "next_sequence"), "2"),
        (("intent", "configuration_id"), str(uuid4())),
        (("profile", "ledger_id"), str(uuid4())),
        (("profile", "archived"), True),
        (("profile", "version"), True),
        (("profile", "version"), "1"),
        (("document", "schema_version"), 2),
        (("document", "asset", "scale"), 18),
        (("document", "asset", "enabled"), True),
        (("document", "current", "document", "total_amount"), "2.00"),
        (("document", "current", "document", "line_count"), 2),
        (("logo", "ledger_id"), str(uuid4())),
        (("logo", "file_id"), str(uuid4())),
        (("logo", "reviewed_version"), 2),
        (("logo", "byte_size"), True),
        (("logo", "byte_size"), "123"),
        (("logo", "byte_size"), 2**63),
        (("logo", "sha256"), "x"),
        (("logo", "media_type"), "application/pdf"),
        (("logo", "path"), "/fictional/untrusted/path"),
        (("logo",), None),
    ],
)
def test_correct_hash_cannot_legitimize_invalid_facts_or_unknown_fields(path, value):
    raw = freeze(*inputs(with_logo=True)).decode()
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(MoneyError):
        read(canonical(raw))


def test_unknown_logo_is_not_added_to_a_profile_that_was_reviewed_without_one():
    raw = freeze(*inputs()).decode()
    raw["logo"] = freeze(*inputs(with_logo=True)).decode()["logo"]
    with pytest.raises(MoneyError):
        read(canonical(raw))


@pytest.mark.parametrize("form", ["spaces", "duplicate", "float", "nan", "infinity", "utf16"])
def test_ambiguous_or_noncanonical_saved_encoding_is_rejected(form):
    frozen = freeze(*inputs())
    raw = frozen.decode()
    if form == "spaces":
        content = json.dumps(raw, ensure_ascii=False, indent=2).encode()
    elif form == "duplicate":
        content = frozen.content.replace(
            b'"template_version":"commercial-invoice-v1"',
            b'"template_version":"commercial-invoice-v1","template_version":"commercial-invoice-v1"',
        )
    elif form == "utf16":
        content = frozen.content.decode().encode("utf-16")
    else:
        raw["document"]["current"]["document"]["issuer_snapshot"]["value"] = {
            "float": 1.0,
            "nan": float("nan"),
            "infinity": float("inf"),
        }[form]
        content = canonical(raw)
    with pytest.raises(MoneyError):
        read(content)


def test_deeply_nested_untrusted_input_is_bounded_and_becomes_a_domain_error():
    content = b"[" * 1500 + b"0" + b"]" * 1500
    with pytest.raises(MoneyError):
        read(content)
