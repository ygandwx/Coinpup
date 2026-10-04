"""Real fictional text and rasters verify preparation, never OCR or financial posting."""

import json
import sys
from dataclasses import replace

import pytest
from coinpup_api.ocr.isolation import ProcessBudget, run_isolated
from coinpup_api.ocr.pdf_prepare import PrepareLimits, prepare_pdf, rendered_page
from coinpup_api.ocr.processor import process

from tests.ocr.pdf_fixtures import document, geometry_document, image_document
from tests.ocr.test_pdf_probe import page_result, source_request

pytestmark = pytest.mark.ocr
TEXT = b"BT /F1 12 Tf 10 60 Td (FICTIONAL USD -0012.30 2026-10-04) Tj ET"
BUDGET_FIELDS = (
    "dpi max_side page_pixels bitmap_bytes document_pixels image_pixels image_sample_bytes "
    "document_image_pixels page_chars document_chars page_text_bytes document_text_bytes "
    "page_words document_words page_evidence_bytes document_evidence_bytes output_bytes"
).split()


@pytest.mark.parametrize("field", BUDGET_FIELDS)
@pytest.mark.parametrize("kind", ["bool", "zero", "above_hard"])
def test_every_preparation_budget_has_strict_explicit_bounds(field, kind):
    value = {
        "bool": True,
        "zero": 0,
        "above_hard": 301 if field == "dpi" else getattr(PrepareLimits(), field) + 1,
    }[kind]
    with pytest.raises(ValueError, match="Invalid PDF preparation limits"):
        replace(PrepareLimits(), **{field: value})


@pytest.mark.parametrize("field,value", [("dpi", 71), ("output_bytes", 127)])
def test_minimum_limits_preserve_a_complete_manual_envelope(field, value):
    with pytest.raises(ValueError):
        replace(PrepareLimits(), **{field: value})
    assert PrepareLimits(output_bytes=128).output_bytes == 128


def prepared(data, **limits):
    return prepare_pdf(data, replace(PrepareLimits(), dpi=72, **limits))


def only_page(result):
    assert result["version"] == 1 and result["page_count"] == 1
    assert result["reason_code"] is None
    assert len(result["pages"]) == 1
    return result["pages"][0]


def manual(page, reason):
    assert page["route"] == "manual" and page["reason_code"] == reason
    assert page["text"] is None and page["words"] == [] and page["raster"] is None


def native_spy(monkeypatch):
    import pypdfium2 as pdfium

    calls, original = [], pdfium.raw.FPDFBitmap_CreateEx

    def observe(*args):
        calls.append(args)
        return original(*args)  # The genuine native allocator, never a replacement bitmap.

    monkeypatch.setattr(pdfium.raw, "FPDFBitmap_CreateEx", observe)
    return calls


def test_real_text_preserves_amount_date_and_word_evidence(monkeypatch):
    allocations = native_spy(monkeypatch)
    page = only_page(prepared(document(TEXT)))
    assert page["layer"] == "present" and page["route"] == "extract"
    assert page["text"] == "FICTIONAL USD -0012.30 2026-10-04"
    assert [word["text"] for word in page["words"]] == [
        "FICTIONAL",
        "USD",
        "-0012.30",
        "2026-10-04",
    ]
    assert all(len(word["bbox"]) == 4 for word in page["words"])
    assert page["raster"] is None and allocations == []


@pytest.mark.parametrize("show", [b"() Tj", b"<FF00FF> Tj"])
def test_empty_or_unmapped_text_is_manual_without_render(monkeypatch, show):
    allocations = native_spy(monkeypatch)
    page = only_page(prepared(document(b"BT /F1 12 Tf " + show + b" ET")))
    assert page["layer"] == "present"
    manual(page, "unusable_text")
    assert allocations == []


def test_unknown_layer_calls_neither_text_extractor_nor_renderer(monkeypatch):
    import pdfplumber
    import pypdfium2 as pdfium

    calls = []

    def unexpected(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Unknown pages must stay manual")

    monkeypatch.setattr(pdfplumber, "open", unexpected)
    monkeypatch.setattr(pdfium, "PdfDocument", unexpected)
    page = only_page(prepared(document(b"FictionalOperator")))
    assert page["layer"] == "unknown" and page["route"] == "manual"
    assert calls == [] and page["text"] is None and page["raster"] is None


@pytest.mark.parametrize(
    "encoding,mode", [("flate", "RGB"), ("flate", "L"), ("jpeg", "RGB"), ("jpeg", "L")]
)
def test_real_supported_image_headers_render_actual_rgbx_rasters(monkeypatch, encoding, mode):
    data = image_document(encoding=encoding, mode=mode)
    allocations = native_spy(monkeypatch)
    page = only_page(prepared(data))
    assert page["layer"] == "absent" and page["route"] == "render"
    assert page["text"] is None and page["words"] == []
    assert page["raster"] == {
        "width": 240,
        "height": 100,
        "stride": 960,
        "format": "RGBX",
        "dpi": 72,
    }
    assert len(allocations) == 1


@pytest.mark.parametrize(
    "limit,value",
    [
        ("max_side", 100),
        ("page_pixels", 100),
        ("bitmap_bytes", 100),
        ("document_pixels", 100),
        ("image_pixels", 10),
        ("image_sample_bytes", 10),
        ("document_image_pixels", 10),
    ],
)
def test_raster_and_image_budgets_reject_before_bitmap_or_decoder(monkeypatch, limit, value):
    from PIL import Image

    data = image_document()
    allocations = native_spy(monkeypatch)
    decoder_calls, original = [], Image.Image.load

    def load(self, *args, **kwargs):
        decoder_calls.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "load", load)
    manual(only_page(prepared(data, **{limit: value})), "prepare_limit")
    assert allocations == [] and decoder_calls == []


@pytest.mark.parametrize("encoding", ["jpeg", "flate"])
def test_dct_header_mismatch_or_flate_sample_underflow_is_manual(monkeypatch, encoding):
    # JPEG has an intrinsic SOF size; Flate has only declared geometry and sample length.
    data = image_document(encoding=encoding, declared_size=(40, 30))
    allocations = native_spy(monkeypatch)
    manual(only_page(prepared(data)), "unsupported_image")
    assert allocations == []


@pytest.mark.parametrize(
    "inherited,attributes,size",
    [
        (b"", b"/Rotate 90 ", (100, 240)),
        (b"/Rotate 90 ", b"", (100, 240)),
        (b"/CropBox [20 10 220 90] /Rotate 90 ", b"", (80, 200)),
        (b"/CropBox [20 10 220 90] ", b"", (200, 80)),
        (b"/CropBox [-10 10 220 120] ", b"", (220, 90)),
    ],
)
def test_rotation_and_inherited_crop_geometry_are_applied_exactly_once(inherited, attributes, size):
    page = only_page(prepared(geometry_document(inherited=inherited, page_attributes=attributes)))
    assert page["route"] == "render"
    assert (page["raster"]["width"], page["raster"]["height"]) == size


@pytest.mark.parametrize(
    "attributes",
    [
        b"/UserUnit 2 ",
        b"/UserUnit 0 ",
        b"/Rotate 45 ",
        b"/CropBox [20 20 10 10] ",
        b"/CropBox [300 300 400 400] ",
    ],
)
def test_invalid_or_unsupported_geometry_never_allocates_a_bitmap(monkeypatch, attributes):
    allocations = native_spy(monkeypatch)
    manual(only_page(prepared(geometry_document(page_attributes=attributes))), "invalid_geometry")
    assert allocations == []


@pytest.mark.parametrize(
    "limit,value",
    [
        ("page_chars", 5),
        ("document_chars", 5),
        ("page_words", 1),
        ("document_words", 1),
        ("page_text_bytes", 5),
        ("document_text_bytes", 5),
        ("page_evidence_bytes", 10),
        ("document_evidence_bytes", 10),
    ],
)
def test_text_or_evidence_exhaustion_is_manual_without_truncation_or_render(
    monkeypatch, limit, value
):
    allocations = native_spy(monkeypatch)
    page = only_page(prepared(document(TEXT), **{limit: value}))
    manual(page, "prepare_limit")
    assert page["layer"] == "present" and allocations == []


def test_whole_json_exhaustion_returns_bounded_manual_result_without_partial_text():
    result = prepared(document(TEXT), output_bytes=128)
    assert len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf8")) <= 128
    assert result["reason_code"] == "prepare_limit"
    assert result["pages"] == []


def test_output_quota_matches_actual_bootstrap_wire_at_the_exact_boundary():
    data = document(TEXT)
    expected = prepared(data)
    assert only_page(expected)["route"] == "extract"
    wire = json.dumps(expected, ensure_ascii=False, allow_nan=False).encode("utf8")
    rejected = prepared(data, output_bytes=len(wire) - 1)
    assert rejected == {
        "version": 1,
        "page_count": None,
        "pages": [],
        "reason_code": "prepare_limit",
    }
    assert (
        len(json.dumps(rejected, ensure_ascii=False, allow_nan=False).encode("utf8"))
        <= len(wire) - 1
    )
    accepted = prepared(data, output_bytes=len(wire))
    assert json.dumps(accepted, ensure_ascii=False, allow_nan=False).encode("utf8") == wire


@pytest.mark.parametrize("limits", [{}, {"page_chars": 5}])
def test_real_text_input_stream_closes_on_success_or_quota_failure(monkeypatch, limits):
    import pdfplumber

    streams, original = [], pdfplumber.open

    def observe(stream, *args, **kwargs):
        streams.append(stream)
        return original(stream, *args, **kwargs)

    monkeypatch.setattr(pdfplumber, "open", observe)
    page = only_page(prepared(document(TEXT), **limits))
    if limits:
        manual(page, "prepare_limit")
    else:
        assert page["route"] == "extract"
    assert len(streams) == 1 and streams[0].closed


def test_utf8_byte_budget_is_independent_of_character_count(monkeypatch):
    from pypdf.generic import NameObject

    def encoding(page, _):
        page["/Resources"]["/Font"]["/F1"][NameObject("/Encoding")] = NameObject("/WinAnsiEncoding")

    data = document(b"BT /F1 12 Tf 10 60 Td (FICTIONAL \xe9\xe9\xe9) Tj ET", mutate=encoding)
    assert only_page(prepared(data))["text"] == "FICTIONAL ééé"
    manual(only_page(prepared(data, page_chars=13, page_text_bytes=13)), "prepare_limit")


def close_spy(monkeypatch):
    import pypdfium2 as pdfium

    events, handles = [], []
    for label, cls in (
        ("bitmap", pdfium.PdfBitmap),
        ("page", pdfium.PdfPage),
        ("document", pdfium.PdfDocument),
    ):
        original = cls.close

        def close(self, *args, _label=label, _original=original, **kwargs):
            if self.raw:
                events.append(_label)
                handles.append(self)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(cls, "close", close)
    return events, handles


def test_success_closes_real_bitmap_page_and_document_in_order(monkeypatch):
    events, handles = close_spy(monkeypatch)
    assert only_page(prepared(image_document()))["route"] == "render"
    assert events.index("bitmap") < len(events) - 1 - events[::-1].index("page")
    assert events[-1] == "document" and all(handle.raw is None for handle in handles)


def test_consumer_exception_closes_real_bitmap_and_page_before_its_document(monkeypatch):
    import pypdfium2 as pdfium

    events, handles = close_spy(monkeypatch)
    with pdfium.PdfDocument(image_document()) as native:
        with pytest.raises(RuntimeError, match="fictional consumer failure"):
            with rendered_page(native, 0, replace(PrepareLimits(), dpi=72)) as bitmap:
                assert bitmap.raw and bitmap.width == 240 and bitmap.height == 100
                raise RuntimeError("fictional consumer failure")
        assert events == ["bitmap", "page"] and native.raw
    assert events == ["bitmap", "page", "document"]
    assert all(handle.raw is None for handle in handles)


def test_dispatcher_keeps_inspection_shape_and_prepares_text_with_the_new_action(tmp_path):
    request = source_request(tmp_path, document(TEXT))
    page_result(process(request, tmp_path), "present", "extract")
    request.update(action="prepare_pdf", prepare_limits={"dpi": 72})
    assert only_page(process(request, tmp_path))["text"] == "FICTIONAL USD -0012.30 2026-10-04"


@pytest.mark.parametrize("case", ["inspect_options", "invalid_limit", "extra"])
def test_dispatcher_rejects_unsupported_preparation_options(tmp_path, case):
    request = source_request(tmp_path, document(TEXT))
    request["prepare_limits"] = {"dpi": 72}
    if case != "inspect_options":
        request["action"] = "prepare_pdf"
        if case == "invalid_limit":
            request["prepare_limits"] = {"output_bytes": 127}
        else:
            request["unexpected"] = "fictional"
    result = process(request, tmp_path)
    assert result["reason_code"] == "request_invalid" and result["pages"] == []


@pytest.mark.skipif(sys.platform != "linux", reason="Requires real Linux isolated preparation")
@pytest.mark.parametrize("kind", ["text", "image"])
def test_real_bootstrap_prepares_fictional_text_or_raster_under_os_limits(tmp_path, kind):
    data = document(TEXT) if kind == "text" else image_document(encoding="jpeg")
    request = source_request(tmp_path, data)
    request.update(action="prepare_pdf", prepare_limits={"dpi": 72})
    budget = ProcessBudget(
        wall_seconds=10,
        cpu_seconds=5,
        address_space_bytes=512 * 1024 * 1024,
        file_bytes=1024 * 1024,
        open_files=64,
        output_bytes=512 * 1024,
    )
    page = only_page(json.loads(run_isolated(request, budget).output))
    if kind == "text":
        assert page["text"] == "FICTIONAL USD -0012.30 2026-10-04" and page["raster"] is None
    else:
        assert page["text"] is None and page["raster"]["format"] == "RGBX"
        assert (page["raster"]["width"], page["raster"]["height"]) == (240, 100)
