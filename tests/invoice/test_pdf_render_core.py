"""Actual optional PDF rendering; all inputs are explicitly fictional frozen evidence."""

import hashlib
import io
import os
import socket
import subprocess
import sys
from decimal import localcontext
from pathlib import Path

import pytest
from coinpup_api.business import invoice_pdf
from coinpup_api.business.invoice_fonts import STATIC_FONTS, load_invoice_fonts
from coinpup_api.business.invoice_render_snapshot import freeze_invoice_render_input
from coinpup_api.ledger.assets import stablecoin_asset
from coinpup_api.ledger.errors import MoneyError

from tests.unit.business.test_invoice_render_snapshot import freeze, inputs
from tests.unit.business.test_invoice_snapshot_reads import canonical

pytestmark = pytest.mark.invoice


@pytest.fixture(scope="module")
def fonts():
    # Missing optional inputs fail this explicit suite; they must never silently skip CI.
    directory = Path(os.environ["COINPUP_INVOICE_FONT_DIR"])
    load_invoice_fonts(directory)
    return directory


def render(frozen, fonts, language="en"):
    return invoice_pdf.render_invoice_text_pdf(
        frozen.content, expected_sha256=frozen.sha256, language=language, font_directory=fonts
    )


def inspect(content):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content), strict=True)
    assert content.startswith(b"%PDF-") and not reader.is_encrypted
    assert 1 <= len(reader.pages) <= invoice_pdf.MAX_INVOICE_PDF_PAGES
    assert len(content) <= invoice_pdf.MAX_INVOICE_PDF_BYTES
    assert "/OpenAction" not in reader.trailer["/Root"]
    for page in reader.pages:
        assert not page.get("/Annots")
        for reference in page["/Resources"]["/Font"].values():
            font = reference.get_object()
            assert "/ToUnicode" in font
            assert len(font["/FontDescriptor"]["/FontFile2"].get_data()) > 1000
    return "\n".join(page.extract_text() for page in reader.pages), len(reader.pages)


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("count", [1, 200])
def test_complete_exact_rows_zero_members_literal_text_and_deterministic_bytes(
    fonts, language, count
):
    facts, command, profile, logo = args = inputs(count)
    facts.document.issuer_snapshot = {"name": "Fictional 虚构发行方", "legal_name": "虛構公司"}
    facts.document.party_snapshot = {"name": "Fictional customer", "address": "  A  B\nC <b>D</b>"}
    profile.name = "Internal profile label must not become the issuer"
    profile.footer = "Fictional final footer"
    for index, row in enumerate(facts.document.lines):
        row.description = f"ROW-{index:03d} 虚构繁體 <script>0</script>"
    amount = "99999999999999999999.99"
    first = facts.document.lines[0]
    first.unit_price = first.net_amount = first.total_amount = amount
    facts.document.net_amount = facts.document.total_amount = amount
    facts.document.tax_amount = first.tax_amount = "0.00"
    frozen = freeze(*args, sequence="9007199254740993")
    before = facts.model_dump(mode="json")
    with localcontext() as context:
        context.prec = 2
        content = render(frozen, fonts, language)
    text, pages = inspect(content)
    assert (pages > 1) == (count == 200)
    from pypdf import PdfReader

    for page in PdfReader(io.BytesIO(content)).pages:
        page_text = page.extract_text()
        # The row description and its amounts must not be orphaned on different pages.
        assert page_text.count("ROW-") == page_text.count(
            "数量:" if language == "zh" else "Quantity:"
        )
    for index in range(count):
        assert text.count(f"ROW-{index:03d}") == 1
    assert text.count(amount) >= 4 and "0.00" in text
    assert "A  B" in text and "C <b>D</b>" in text and "<script>0</script>" in text
    assert "Fictional 虚构发行方" in text and "虛構公司" in text
    assert profile.name not in text and "示例-INV-9007199254740993" in text
    assert text.rfind("Fictional final footer") > text.rfind(f"ROW-{count - 1:03d}")
    assert "商业发票" in text if language == "zh" else "COMMERCIAL INVOICE" in text
    assert facts.model_dump(mode="json") == before
    facts.document.lines.clear()
    profile.footer = "Later current data must not replace saved bytes"
    assert render(frozen, fonts, language) == content


@pytest.mark.parametrize("language", ["zh", "en"])
def test_all_200_maximum_length_descriptions_survive_every_page(fonts, language):
    args = inputs(200)
    for index, row in enumerate(args[0].document.lines):
        marker = f"BEGIN-{index:03d} "
        row.description = marker + "虚构測試文本" * ((2000 - len(marker) - 8) // 6)
        row.description += "x" * (1992 - len(row.description)) + f" END-{index:03d}"
        assert len(row.description) == 2000
    # Exercise multiple embedded subsets and the TC fallback. These two supplementary CJK
    # characters have real glyphs; a visually correct PDF must also recover the source text.
    marker = "MAP-𠂇𠀡-END"
    sample = "".join(chr(i) for i in range(0x4E00, 0x4F20)) + marker
    first = args[0].document.lines[0]
    first.description = first.description[:10] + sample + first.description[10 + len(sample) :]
    assert len(first.description) == 2000
    args[0].document.notes = marker
    text, pages = inspect(render(freeze(*args), fonts, language))
    compact = "".join(text.split())
    assert compact.count(marker) == 2
    assert pages > 100
    for index in range(200):
        assert compact.count(f"BEGIN-{index:03d}") == compact.count(f"END-{index:03d}") == 1
    assert "Fictionalfooter" in compact


@pytest.mark.parametrize("scale", [0, 6, 18])
def test_minimum_unit_asset_network_and_case_sensitive_identity_stay_exact(fonts, scale):
    facts, command, profile, logo = inputs(1)
    asset = stablecoin_asset(
        code="USDT", network="fictional", token_reference="CaseSensitive", scale=scale
    )
    amount = "1" if scale == 0 else "0." + "0" * (scale - 1) + "1"
    facts.document.asset_id = asset.asset_id
    row = facts.document.lines[0]
    row.asset_id = asset.asset_id
    row.unit_price = row.net_amount = row.total_amount = amount
    facts.document.net_amount = facts.document.total_amount = amount
    frozen = freeze_invoice_render_input(
        facts, asset, profile, ledger_id=facts.ledger_id, command=command, sequence="1", logo=logo
    )
    text, _ = inspect(render(frozen, fonts))
    assert text.count(amount) >= 4
    assert "USDT" in text and "fictional" in text and "CaseSensitive" in text


@pytest.mark.parametrize("language", [None, [], True, "EN", "fr"])
def test_unknown_language_rejects_before_font_io(language, tmp_path):
    with pytest.raises(MoneyError) as caught:
        render(freeze(*inputs()), tmp_path, language)
    assert caught.value.code == "invoice_render_invalid"


def test_wrong_owned_digest_and_template_reject_before_optional_rendering(tmp_path):
    frozen = freeze(*inputs())
    raw = frozen.decode()
    raw["template_version"] = "unreviewed-template"
    for content, digest in [
        (frozen.content, "f" * 64),
        (canonical(raw), hashlib.sha256(canonical(raw)).hexdigest()),
    ]:
        with pytest.raises(MoneyError) as caught:
            invoice_pdf.render_invoice_text_pdf(
                content, expected_sha256=digest, language="en", font_directory=tmp_path
            )
        assert caught.value.code == "invoice_render_integrity"


def test_logo_cannot_be_silently_omitted_from_the_text_only_stage(tmp_path):
    with pytest.raises(MoneyError) as caught:
        render(freeze(*inputs(with_logo=True)), tmp_path)
    assert caught.value.code == "invoice_render_logo_unavailable"


@pytest.mark.parametrize("character", ["\U0010ffff", "\t"])
@pytest.mark.parametrize("placement", ["footer", "row"])
def test_unsupported_glyph_or_control_never_becomes_substitute_boxes(fonts, character, placement):
    args = inputs(1)
    if placement == "footer":
        args[2].footer = "Fictional " + character
    else:
        args[0].document.lines[0].description = "Fictional " + character + " end"
    with pytest.raises(MoneyError) as caught:
        render(freeze(*args), fonts)
    assert caught.value.code == "invoice_render_glyph"


def test_nul_is_rejected_by_frozen_input_validation_before_rendering(fonts):
    args = inputs(1)
    args[2].footer = "Fictional \x00"
    with pytest.raises(MoneyError) as caught:
        render(freeze(*args), fonts)
    assert caught.value.code == "invoice_render_integrity"


def test_unreviewed_snapshot_fields_fail_closed(fonts):
    args = inputs(1)
    args[0].document.issuer_snapshot["unreviewed_field"] = "Fictional"
    with pytest.raises(MoneyError) as caught:
        render(freeze(*args), fonts)
    assert caught.value.code == "invoice_render_invalid"


@pytest.mark.parametrize("limit", ["MAX_INVOICE_PDF_BYTES", "MAX_INVOICE_PDF_PAGES"])
def test_real_layout_and_output_limits_fail_without_returning_partial_bytes(
    fonts, monkeypatch, limit
):
    monkeypatch.setattr(invoice_pdf, limit, 1)
    with pytest.raises(MoneyError) as caught:
        render(freeze(*inputs(200)), fonts)
    assert caught.value.code == "invoice_render_limit"


def test_missing_or_changed_font_has_no_system_font_fallback(fonts, tmp_path):
    frozen = freeze(*inputs(1))
    for name in [*STATIC_FONTS, None]:
        if name is not None:
            (tmp_path / name.filename).write_bytes(b"broken pinned font")
        with pytest.raises(MoneyError) as caught:
            render(frozen, tmp_path)
        assert caught.value.code == "invoice_fonts_unavailable"
        assert str(tmp_path) not in str(caught.value)


def test_renderer_does_not_connect_to_network(fonts, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Frozen Invoice rendering must not access a network")

    monkeypatch.setattr(socket, "socket", forbidden)
    text, _ = inspect(render(freeze(*inputs(1)), fonts))
    assert "Fictional" in text


def test_importing_core_does_not_load_pdf_image_or_font_builder_dependencies():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import coinpup_api.business.invoice_pdf; "
            "assert not {'reportlab', 'PIL', 'fontTools', 'pypdf'} & sys.modules.keys()",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
