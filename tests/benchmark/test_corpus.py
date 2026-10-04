"""Actual fictional carriers and independent truth checks; no OCR-engine substitutes."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

import pytest

pytestmark = [pytest.mark.ocr, pytest.mark.benchmark]
ROOT = Path(__file__).resolve().parents[2]
INPUTS = ROOT / "tests/fixtures/ocr"
TEMPLATES = ("S01", "S02", "S03", "T01", "T02", "T03", "E01", "E02", "E03", "M01", "M02", "M03")
FILE_ERRORS = ("damaged", "encrypted", "page_limit", "pixel_limit", "stream_limit", "garbled_layer")


def _read(path):
    return json.loads(path.read_text(encoding="utf8"))


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _file(directory, relative):
    assert type(relative) is str and "\\" not in relative
    path = PurePosixPath(relative)
    assert not path.is_absolute() and ".." not in path.parts
    target = directory / relative
    assert target.resolve().is_relative_to(directory.resolve()) and not target.is_symlink()
    assert target.is_file()
    return target


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    configured = os.environ.get("COINPUP_CORPUS_SOURCES")
    assert configured, "COINPUP_CORPUS_SOURCES must identify the pinned font sources."
    source = Path(configured).resolve()
    assert source.is_dir()
    base = tmp_path_factory.mktemp("corpus-processes")
    program = (
        "import json,sys; from scripts.ocr_benchmark.corpus import generate_corpus; "
        "print(json.dumps(generate_corpus(sys.argv[1],sys.argv[2]),sort_keys=True))"
    )
    records = []
    for index in range(2):
        directory = base / str(index)
        environment = dict(
            os.environ,
            PYTHONIOENCODING="utf8",
            PYTHONHASHSEED=str(index + 1),
            SOURCE_DATE_EPOCH=str(index + 1),
        )
        result = subprocess.run(
            [sys.executable, "-c", program, str(source), str(directory)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf8",
            check=True,
            timeout=240,
        )
        records.append((directory, json.loads(result.stdout)))
    return records


def _case(corpus, identity):
    directory, manifest = corpus[0]
    case = next(case for case in manifest["cases"] if case["id"] == identity)
    return case, _file(directory, case["relative_path"]).read_bytes()


def _template(identity):
    return next(
        item for item in _read(INPUTS / "truth.json")["templates"] if item["id"] == identity
    )


def _expected(template, carrier):
    result = {"header." + role: value for role, value in template["header"].items()}
    continued_row = 0
    for page, source in enumerate(template["pages"]):
        for row, cells in enumerate(source["rows"]):
            physical_page, physical_row = (0, continued_row) if carrier == "photo" else (page, row)
            prefix = f"rows.{physical_page}.0.{physical_row}."
            result.update({prefix + role: cells[role] for role in ("date", "currency", "amount")})
            continued_row += 1
    return result


def _printed(template):
    pages = []
    for page in template["pages"]:
        rows = [
            " | ".join(row[key] for key in ("date", "currency", "raw_amount", "description"))
            for row in page["rows"]
        ]
        pages.append("\n".join([*page["lines"], *rows]))
    return "\n\n".join(pages)


def _raster_hashes(data, media_type):
    if media_type == "application/pdf":
        import pypdfium2
        from coinpup_api.ocr.pdf_prepare import PrepareLimits, rendered_page

        hashes = []
        with pypdfium2.PdfDocument(data) as document:
            for index in range(len(document)):
                with rendered_page(document, index, PrepareLimits(dpi=300)) as bitmap:
                    with bitmap.to_pil() as image, image.convert("RGB") as rgb:
                        hashes.append(_digest(rgb.tobytes()))
        return hashes
    from coinpup_api.ocr.image_prepare import prepared_image

    with prepared_image(data, media_type) as raster, raster.convert("RGB") as rgb:
        return [_digest(rgb.tobytes())]


def _parse_pdf(data):
    from coinpup_api.ocr.field_parser import parse_document
    from coinpup_api.ocr.parser_types import TextPage, Word
    from coinpup_api.ocr.pdf_prepare import PrepareLimits, prepare_pdf
    from pypdf import PdfReader

    prepared, inputs = prepare_pdf(data, PrepareLimits(dpi=300)), []
    with io.BytesIO(data) as stream:
        actual = PdfReader(stream)
        for page, geometry in zip(prepared["pages"], actual.pages, strict=True):
            inputs.append(
                TextPage(
                    float(geometry.mediabox.width),
                    float(geometry.mediabox.height),
                    tuple(Word(word["text"], tuple(word["bbox"])) for word in page["words"]),
                    page["text"],
                )
            )
    # Only genuine source words and measured page geometry enter the shared parser.
    return prepared, parse_document(tuple(inputs))


def test_complete_manifest_and_all_actual_asset_bytes(corpus):
    from scripts.ocr_benchmark.corpus import verify_corpus

    directory, manifest = corpus[0]
    assert manifest["version"] == 1 and manifest["seed"] == 20261004
    assert Counter(case["group"] for case in manifest["cases"]) == {
        "text": 12,
        "ocr": 24,
        "degraded": 12,
        "error": 8,
    }
    assert len({case["id"] for case in manifest["cases"]}) == 56
    development = manifest["development"]
    assert len(development) == 4
    assert {case["id"] for case in development}.isdisjoint(case["id"] for case in manifest["cases"])
    for key, filename in (
        ("truth_sha256", "truth.json"),
        ("configuration_sha256", "corpus-config.json"),
        ("development_sha256", "development.json"),
    ):
        assert manifest[key] == _digest((INPUTS / filename).read_bytes())
    assert manifest["fonts_sha256"] == _digest((directory / "fonts/fonts.json").read_bytes())
    assert {item["relative_path"] for item in manifest["generator_sources"]} == {
        "scripts/ocr_benchmark/corpus.py",
        "scripts/ocr_benchmark/fonts.py",
    }
    for item in manifest["generator_sources"]:
        source = _file(ROOT, item["relative_path"]).read_bytes()
        assert (item["sha256"], item["byte_size"]) == (_digest(source), len(source))
    assert manifest["dependency_lock"]["sha256"] == _digest(
        (ROOT / "requirements-benchmark.lock").read_bytes()
    )
    assets = {asset["relative_path"]: asset for asset in manifest["assets"]}
    assert len(assets) == len(manifest["assets"])
    assert set(assets) == {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    for asset in assets.values():
        data = _file(directory, asset["relative_path"]).read_bytes()
        assert len(data) == asset["byte_size"] and _digest(data) == asset["sha256"]
    for case in [*manifest["cases"], *development]:
        asset = assets[case["relative_path"]]
        assert (case["sha256"], case["byte_size"]) == (asset["sha256"], asset["byte_size"])
        assert case["byte_size"] <= 20 * 1024 * 1024
        assert case["raster_mode"] == "RGB" and case["raster_dpi"] == 300
    assert verify_corpus(directory) == manifest


def test_full_fresh_process_generations_are_byte_identical(corpus):
    (first, left), (second, right) = corpus
    assert first != second and left == right
    assert (first / "manifest.json").read_bytes() == (second / "manifest.json").read_bytes()
    for asset in left["assets"]:
        relative = asset["relative_path"]
        assert _file(first, relative).read_bytes() == _file(second, relative).read_bytes()


@pytest.mark.parametrize(
    "damage", ["modified_bytes", "extra_file", "duplicate_asset", "escape_path"]
)
def test_verification_rejects_changed_or_unbounded_artifacts(corpus, tmp_path, damage):
    from scripts.ocr_benchmark.corpus import CorpusError, verify_corpus

    directory = tmp_path / "damaged"
    shutil.copytree(corpus[0][0], directory)
    manifest = _read(directory / "manifest.json")
    if damage == "modified_bytes":
        path = _file(directory, manifest["assets"][0]["relative_path"])
        with path.open("r+b") as stream:
            first = stream.read(1)
            stream.seek(0)
            stream.write(bytes([first[0] ^ 1]))
    elif damage == "extra_file":
        (directory / "unlisted.txt").write_text("FICTIONAL EXTRA ARTIFACT", encoding="utf8")
    else:
        if damage == "duplicate_asset":
            manifest["assets"].append(dict(manifest["assets"][0]))
        else:
            manifest["assets"][0]["relative_path"] = "../escaped.pdf"
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
    with pytest.raises(CorpusError):
        verify_corpus(directory)


@pytest.mark.parametrize("identity", TEMPLATES)
def test_actual_text_carrier_complete_printed_text_and_hundred_percent_fields(corpus, identity):
    from scripts.ocr_benchmark.scoring import score_fields

    case, data = _case(corpus, identity + "-text")
    template = _template(identity)
    expected = _expected(template, "text_pdf")
    assert case["reference_text"] == _printed(template)
    assert case["expected_fields"] == expected
    assert case["carrier"] == "text_pdf" and case["dimensions"] == [[420, 595]] * len(
        template["pages"]
    )
    prepared, parsed = _parse_pdf(data)
    assert prepared["reason_code"] is None and prepared["page_count"] == len(template["pages"])
    assert all(
        page["layer"] == "present"
        and page["route"] == "extract"
        and page["reason_code"] is None
        and page["raster"] is None
        for page in prepared["pages"]
    )
    assert "\n\n".join(page["text"] for page in prepared["pages"]) == _printed(template)
    predicted = [
        {key: field[key] for key in ("path", "value", "status")} for field in parsed["fields"]
    ]
    score = score_fields(expected, predicted)
    assert (score.correct, score.expected, score.extra) == (len(expected), len(expected), 0), parsed
    assert parsed["status"] == "parsed" and all(
        field["status"] == "certain" for field in parsed["fields"]
    )


def test_actual_text_gate_has_all_153_critical_fields_and_no_extra_predictions(corpus, capsys):
    from scripts.ocr_benchmark.scoring import aggregate_scores, score_fields

    scores = []
    for identity in TEMPLATES:
        _, data = _case(corpus, identity + "-text")
        _, parsed = _parse_pdf(data)
        predicted = [
            {key: field[key] for key in ("path", "value", "status")} for field in parsed["fields"]
        ]
        scores.append(score_fields(_expected(_template(identity), "text_pdf"), predicted))
    score = aggregate_scores(scores)
    with capsys.disabled():
        print(
            "CORPUS_TEXT_GATE "
            + json.dumps(
                {"documents": 12, "C": score.correct, "T": score.expected, "E": score.extra},
                sort_keys=True,
            )
        )
    assert (score.correct, score.expected, score.extra) == (153, 153, 0)


@pytest.mark.parametrize("identity", TEMPLATES)
def test_actual_scan_is_image_only_dct_at_native_300_dpi(corpus, identity):
    from coinpup_api.ocr.pdf_prepare import PrepareLimits, prepare_pdf
    from coinpup_api.ocr.pdf_probe import probe_pdf
    from pypdf import PdfReader
    from pypdf.generic import ContentStream

    case, data = _case(corpus, identity + "-scan")
    template = _template(identity)
    assert case["carrier"] == "scan_pdf" and case["expected_fields"] == _expected(
        template, "scan_pdf"
    )
    probe = probe_pdf(data)
    assert probe["reason_code"] is None and probe["page_count"] == len(template["pages"])
    assert all(page["layer"] == "absent" and page["route"] == "render" for page in probe["pages"])
    with io.BytesIO(data) as stream:
        reader = PdfReader(stream)
        for page in reader.pages:
            operations = ContentStream(page.get_contents(), reader).operations
            assert not any(operator == b"INLINE IMAGE" for _, operator in operations)
            images = [
                obj.get_object()
                for obj in page["/Resources"]["/XObject"].values()
                if obj.get_object().get("/Subtype") == "/Image"
            ]
            assert images and all(str(image["/Filter"]) == "/DCTDecode" for image in images)
    prepared = prepare_pdf(data, PrepareLimits(dpi=300))
    assert all(
        page["route"] == "render"
        and page["reason_code"] is None
        and page["text"] is None
        and page["words"] == []
        for page in prepared["pages"]
    )
    assert all(
        page["raster"]
        == {"width": 1751, "height": 2480, "stride": 7004, "format": "RGBX", "dpi": 300}
        for page in prepared["pages"]
    )
    assert _raster_hashes(data, case["media_type"]) == case["raster_sha256"]


@pytest.mark.parametrize("identity", TEMPLATES)
def test_photo_preserves_all_source_pages_and_is_upright_within_real_budgets(corpus, identity):
    from coinpup_api.ocr.image_prepare import ImageLimits, prepare_image
    from coinpup_api.ocr.pdf_prepare import PrepareLimits
    from PIL import Image

    case, data = _case(corpus, identity + "-photo")
    template = _template(identity)
    height = 2480 * len(template["pages"])
    assert case["carrier"] == "photo" and case["page_count"] == 1
    assert case["dimensions"] == [[1751, height]]
    assert case["expected_fields"] == _expected(template, "photo")
    assert case["reference_text"] == _printed(template)
    with io.BytesIO(data) as stream, Image.open(stream) as image:
        assert image.size == (1751, height) and getattr(image, "n_frames", 1) == 1
        assert not image.getexif() and "icc_profile" not in image.info
        image.load()
        for page in range(len(template["pages"])):
            with image.crop((0, page * 2480, 1751, (page + 1) * 2480)) as physical:
                assert min(channel[0] for channel in physical.getextrema()) < 200
    assert 1751 * height <= PrepareLimits().page_pixels
    assert 16 * 1751 * height <= ImageLimits().working_bytes
    prepared = prepare_image(data, case["media_type"])
    assert prepared["pages"][0]["route"] == "render" and prepared["pages"][0]["reason_code"] is None
    assert prepared["pages"][0]["raster"]["height"] == height
    assert _raster_hashes(data, case["media_type"]) == case["raster_sha256"]
    mappings = case["source_mapping"]
    assert [mapping["source_page"] for mapping in mappings["pages"]] == list(
        range(len(template["pages"]))
    )
    assert all(mapping["page"] == 0 for mapping in mappings["pages"])
    assert [mapping["offset"] for mapping in mappings["pages"]] == [
        [0, page * 2480] for page in range(len(template["pages"]))
    ]
    assert [row["row"] for row in mappings["rows"]] == list(
        range(sum(len(p["rows"]) for p in template["pages"]))
    )


def test_degraded_group_preserves_twelve_cases_and_three_explicit_review_scenarios(corpus):
    from coinpup_api.ocr.image_prepare import prepare_image

    cases = [case for case in corpus[0][1]["cases"] if case["group"] == "degraded"]
    assert {case["template_id"] for case in cases} == set(TEMPLATES)
    assert Counter(case["perturbation"]["kind"] for case in cases) == {
        "rotate": 3,
        "contrast": 3,
        "blur": 3,
        "perspective": 3,
    }
    expected_reviews = {
        "E01": ["header.document_date"],
        "T02": ["header.currency"],
        "M02": ["header.total"],
    }
    for case in cases:
        assert case["expected_review_paths"] == expected_reviews.get(case["template_id"], [])
        assert case["expected_fields"] == _expected(_template(case["template_id"]), "photo")
        data = _file(corpus[0][0], case["relative_path"]).read_bytes()
        assert prepare_image(data, case["media_type"])["pages"][0]["route"] == "render"
    by_template = {case["template_id"]: case for case in cases}
    assert "Date: 01/02/2026" in by_template["E01"]["reference_text"]
    assert "幣種: $" in by_template["T02"]["reference_text"]
    assert "合计 Total: 10.00\n合计 Total: 11.00" in by_template["M02"]["reference_text"]


@pytest.mark.parametrize("kind", FILE_ERRORS)
def test_actual_file_error_carriers_never_render_or_hide_an_existing_layer(corpus, kind):
    from coinpup_api.ocr.pdf_prepare import PrepareLimits, prepare_pdf
    from coinpup_api.ocr.pdf_probe import probe_pdf

    case, data = _case(corpus, "error-" + kind)
    assert case["group"] == "error" and case["runtime_scenario"] is None
    probe = probe_pdf(data)
    prepared = prepare_pdf(data, PrepareLimits(dpi=300))
    if kind == "garbled_layer":
        assert any(page["layer"] == "present" for page in probe["pages"])
        assert prepared["pages"] and all(page["layer"] == "present" for page in prepared["pages"])
        assert all(
            page["route"] == "manual" and page["raster"] is None for page in prepared["pages"]
        )
    else:
        assert prepared["reason_code"] is not None or any(
            page["reason_code"] for page in prepared["pages"]
        )
        assert all(
            page["route"] == "manual" and page["raster"] is None for page in prepared["pages"]
        )
    if kind == "encrypted":
        assert probe["reason_code"] == "encrypted_pdf"
    if kind == "page_limit":
        assert probe["reason_code"] == "probe_limit"
    if kind == "stream_limit":
        assert probe["reason_code"] == "probe_limit" or any(
            page["reason_code"] == "probe_limit" for page in probe["pages"]
        )
    if kind == "pixel_limit":
        assert any(page["reason_code"] == "prepare_limit" for page in prepared["pages"])


@pytest.mark.parametrize("kind", ["missing_model", "timeout"])
def test_runtime_error_cases_have_real_valid_sources_without_fake_engine_results(corpus, kind):
    from coinpup_api.ocr.pdf_prepare import PrepareLimits, prepare_pdf

    case, data = _case(corpus, "error-" + kind)
    assert case["group"] == "error" and case["runtime_scenario"] is not None
    assert "measured_result" not in case
    prepared = prepare_pdf(data, PrepareLimits(dpi=300))
    assert prepared["reason_code"] is None
    assert prepared["pages"] and all(
        page["route"] == "render" and page["reason_code"] is None for page in prepared["pages"]
    )
