"""Real corpus font tooling; these fictional lines are not acceptance truth."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.ocr, pytest.mark.benchmark]

LINES = (
    ("SC", "虚构测试票据 / FICTIONAL TEST DOCUMENT"),
    ("TC", "虛構測試票據 / FICTIONAL TEST DOCUMENT"),
    ("SC", "日期: 2032-03-04"),
    ("TC", "幣種: USD"),
    ("SC", "合计: -12.30"),
)
CHARACTERS = sorted(set("".join(text for _, text in LINES)))
FILES = ("NotoSansSC-VF.ttf", "NotoSansTC-VF.ttf", "LICENSE")
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def source_dir():
    configured = os.environ.get("COINPUP_CORPUS_SOURCES")
    assert configured, "COINPUP_CORPUS_SOURCES must identify the pinned font sources."
    directory = Path(configured).resolve()
    assert directory.is_dir(), "The configured font source directory must exist."
    return directory


@pytest.fixture(scope="module")
def built(source_dir, tmp_path_factory):
    from scripts.ocr_benchmark.fonts import prepare_fonts

    directory = tmp_path_factory.mktemp("static-fonts")
    return directory, prepare_fonts(source_dir, directory, CHARACTERS)


def test_source_verification_is_offline_without_build_imports(source_dir):
    program = (
        "import json,sys; from scripts.ocr_benchmark.fonts import verify_sources; "
        "result=verify_sources(sys.argv[1]); "
        "assert not any(name.startswith(('fontTools','urllib.request')) "
        "for name in sys.modules); print(json.dumps(result))"
    )
    result = subprocess.run(
        [sys.executable, "-c", program, str(source_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    verified = json.loads(result.stdout)
    assert {item["name"] for item in verified["files"]} == set(FILES)
    assert len(verified["source_revision"]) == 40


@pytest.mark.parametrize("filename", FILES)
@pytest.mark.parametrize("damage", ["missing", "same_size_mutation"])
def test_missing_or_modified_sources_and_license_are_rejected(
    source_dir, tmp_path, filename, damage
):
    from scripts.ocr_benchmark.fonts import FontBuildError, verify_sources

    for name in FILES:
        if name != filename or damage != "missing":
            shutil.copyfile(source_dir / name, tmp_path / name)
    if damage == "same_size_mutation":
        with (tmp_path / filename).open("r+b") as stream:
            first = stream.read(1)
            stream.seek(0)
            stream.write(bytes([first[0] ^ 1]))
    with pytest.raises(FontBuildError) as error:
        verify_sources(tmp_path)
    assert error.value.code == "font_source_invalid"


def test_static_fonts_have_real_glyphs_and_preserve_licensing(source_dir, built):
    from fontTools.ttLib import TTFont

    directory, metadata = built
    assert json.loads((directory / "fonts.json").read_text(encoding="utf8")) == metadata
    assert (directory / "LICENSE").read_bytes() == (source_dir / "LICENSE").read_bytes()
    covered = set()
    for record in metadata["fonts"]:
        path = directory / record["filename"]
        data = path.read_bytes()
        assert len(data) == record["byte_size"]
        assert hashlib.sha256(data).hexdigest() == record["sha256"]
        with (
            TTFont(source_dir / f"NotoSans{record['role']}-VF.ttf") as source,
            TTFont(path) as font,
        ):
            assert "glyf" in font and "CFF " not in font
            assert not ({"fvar", "gvar", "avar", "HVAR", "MVAR", "STAT"} & set(font.keys()))
            assert font["OS/2"].usWeightClass == 400
            for name_id in (0, 13, 14):
                original = sorted(
                    (n.platformID, n.platEncID, n.langID, n.string)
                    for n in source["name"].names
                    if n.nameID == name_id
                )
                retained = sorted(
                    (n.platformID, n.platEncID, n.langID, n.string)
                    for n in font["name"].names
                    if n.nameID == name_id
                )
                assert retained == original
            for name_id in (1, 4, 6):
                assert {n.toUnicode() for n in font["name"].names if n.nameID == name_id} == {
                    "CoinpupCorpus" + record["role"]
                }
            cmap = font.getBestCmap()
            assert sorted(cmap) == record["codepoints"]
            assert all(font.getGlyphID(name) != 0 for name in cmap.values())
            covered.update(cmap)
    assert set(map(ord, CHARACTERS)) <= covered


def test_whole_line_font_selection_and_missing_glyph_fail(source_dir, built, tmp_path):
    from scripts.ocr_benchmark.fonts import FontBuildError, font_for, prepare_fonts

    _, metadata = built
    assert font_for("虛構測試票據", "TC", metadata)["role"] == "TC"
    assert font_for("测虚试", "TC", metadata)["role"] == "SC"
    with pytest.raises(FontBuildError) as selection:
        font_for("\U0010ffff", "SC", metadata)
    assert selection.value.code == "font_coverage"
    with pytest.raises(FontBuildError) as construction:
        prepare_fonts(source_dir, tmp_path, ["\U0001f9d0"])
    assert construction.value.code == "font_coverage"
    assert not (tmp_path / "fonts.json").exists()


def test_independent_processes_build_identical_fonts_at_different_paths(source_dir, tmp_path):
    program = (
        "import json,sys; from scripts.ocr_benchmark.fonts import prepare_fonts; "
        "print(json.dumps(prepare_fonts(sys.argv[1],sys.argv[2],json.loads(sys.argv[3])),"
        "ensure_ascii=False,sort_keys=True))"
    )
    records = []
    for index, characters in enumerate((CHARACTERS, list(reversed(CHARACTERS)))):
        output = tmp_path / str(index)
        result = subprocess.run(
            [sys.executable, "-c", program, str(source_dir), str(output), json.dumps(characters)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        records.append((output, json.loads(result.stdout)))
    assert records[0][1] == records[1][1]
    for name in [record["filename"] for record in records[0][1]["fonts"]] + ["fonts.json"]:
        assert (records[0][0] / name).read_bytes() == (records[1][0] / name).read_bytes()


def test_reportlab_cjk_pdf_is_readable_by_real_probe_extractor_and_renderer(built):
    import pdfplumber
    import pypdfium2
    from coinpup_api.ocr.pdf_probe import probe_pdf
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen.canvas import Canvas

    from scripts.ocr_benchmark.fonts import font_for

    directory, metadata = built
    for record in metadata["fonts"]:
        pdfmetrics.registerFont(TTFont(record["name"], directory / record["filename"]))
    with io.BytesIO() as stream:
        canvas = Canvas(stream, pagesize=(420, 595), invariant=1, pageCompression=1)
        for index, (primary, text) in enumerate(LINES):
            canvas.setFont(font_for(text, primary, metadata)["name"], 12)
            canvas.drawString(30, 555 - index * 30, text)
        canvas.save()
        data = stream.getvalue()
    probe = probe_pdf(data)
    assert probe["reason_code"] is None and probe["page_count"] == 1
    assert probe["pages"][0]["layer"] == "present"
    assert probe["pages"][0]["route"] == "extract"
    with io.BytesIO(data) as stream, pdfplumber.open(stream) as document:
        extracted = document.pages[0].extract_text()
        assert all(text in extracted for _, text in LINES)
    with pypdfium2.PdfDocument(data) as document:
        page = document[0]
        try:
            bitmap = page.render(scale=1)
            try:
                assert (bitmap.width, bitmap.height) == (420, 595)
                assert len(bitmap.buffer) > 0
                with bitmap.to_pil() as image:
                    for top in (25, 55):
                        with image.crop((30, top, 102, top + 20)) as glyphs:
                            assert min(channel[0] for channel in glyphs.getextrema()) < 255
            finally:
                bitmap.close()
        finally:
            page.close()
