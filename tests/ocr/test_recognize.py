"""Real preparation/shared parsing with an explicitly fake adapter, never SDK measurements."""

import hashlib
import json
from dataclasses import replace
from io import BytesIO

import pytest
from coinpup_api.ocr import recognize
from coinpup_api.ocr.engine_adapters import EngineError
from coinpup_api.ocr.parser_types import TextPage, Word
from coinpup_api.ocr.pdf_prepare import PrepareLimits, prepare_pdf

from tests.ocr.pdf_fixtures import document, image_document

pytestmark = pytest.mark.ocr
TEXT = b"BT /F1 12 Tf 10 60 Td (Total: -001,234.00) Tj ET"


class FakeAdapter:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def recognize(self, rgb, width, height):
        self.calls.append((rgb, width, height))
        if self.error is not None:
            raise self.error
        return TextPage(
            width,
            height,
            (Word("Total: -001,234.00", (0, 0, width, min(height, 10))),),
            "Total: -001,234.00",
        )


def native_spy(monkeypatch):
    import pypdfium2 as pdfium

    actual, original = [], pdfium.PdfBitmap.new_native

    def allocate(*args, **kwargs):
        bitmap = original(*args, **kwargs)
        actual.append(bitmap)
        return bitmap

    monkeypatch.setattr(pdfium.PdfBitmap, "new_native", allocate)
    return actual


@pytest.mark.parametrize("kind", ["text", "raster"])
def test_optional_callbacks_preserve_the_exact_default_result(monkeypatch, kind):
    data = document(TEXT) if kind == "text" else image_document()
    expected = prepare_pdf(data, PrepareLimits(dpi=300))
    observed, completed = [], []

    def consume(index, source):
        assert index == 0
        if isinstance(source, TextPage):
            assert source.text == "Total: -001,234.00" and source.width == 240
        else:
            assert source.raw is not None and source.width == 1001 and source.height == 417
        observed.append(source)

    actual = prepare_pdf(
        data, PrepareLimits(dpi=300), _page_consumer=consume, _page_done=completed.append
    )
    assert json.dumps(actual, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)
    assert completed == [0] and len(observed) == 1
    if kind == "raster":
        assert observed[0].raw is None


def test_actual_text_layer_calls_no_engine_and_preserves_original_amount(monkeypatch):
    allocations = native_spy(monkeypatch)
    adapter, done = FakeAdapter(), []
    result = recognize.recognize_document(
        document(TEXT), "application/pdf", adapter, page_done=done.append
    )
    assert adapter.calls == [] and allocations == [] and done == [0]
    assert result["status"] == "processed" and result["raw_text"] == "Total: -001,234.00"
    assert result["parsed"]["fields"][0]["value"] == "-1234.00"
    page = result["pages"][0]
    assert page["geometry"] == {"width": 240, "height": 100, "units": "pt"}
    assert page["raster_rgb_sha256"] is None and result["timings_ns"]["recognize"] == 0


def test_cropped_text_keeps_original_default_extraction_but_recognition_is_manual():
    from pypdf.generic import ArrayObject, NameObject, NumberObject

    def crop(page, _):
        page[NameObject("/CropBox")] = ArrayObject([NumberObject(v) for v in (60, 0, 240, 100)])

    data = document(TEXT, mutate=crop)
    original = prepare_pdf(data)
    assert original["pages"][0]["route"] == "extract"
    assert original["pages"][0]["text"] == "Total: -001,234.00"
    adapter, done = FakeAdapter(), []
    result = recognize.recognize_document(data, "application/pdf", adapter, page_done=done.append)
    assert result["status"] == "manual" and result["reason"] == "invalid_geometry"
    assert result["pages"][0]["layer"] == "present" and result["pages"][0]["route"] == "manual"
    assert adapter.calls == [] and result["parsed"]["fields"] == [] and done == [0]


@pytest.mark.parametrize(
    "contents",
    [b"FictionalUnknownOperator", b"BT /F1 12 Tf () Tj ET", b"BT /F1 12 Tf <FF00FF> Tj ET"],
)
def test_unknown_or_unusable_text_never_falls_back_to_ocr(monkeypatch, contents):
    allocations, adapter, done = native_spy(monkeypatch), FakeAdapter(), []
    result = recognize.recognize_document(
        document(contents), "application/pdf", adapter, page_done=done.append
    )
    assert result["status"] == "manual" and result["pages"][0]["route"] == "manual"
    assert result["parsed"]["fields"] == [] and result["raw_text"] == ""
    assert allocations == [] and adapter.calls == [] and done == [0]


def test_real_raster_is_rendered_once_live_and_sha_is_the_actual_rgb_input(monkeypatch):
    allocations, adapter, done, observed = native_spy(monkeypatch), FakeAdapter(), [], []

    def completed(index):
        assert allocations[0].raw is None
        done.append(index)

    def live_observer():
        assert allocations[0].raw is not None
        observed.append(True)

    result = recognize.recognize_document(
        image_document(),
        "application/pdf",
        adapter,
        page_done=completed,
        _observe_native=live_observer,
    )
    assert result["status"] == "processed" and len(allocations) == len(adapter.calls) == 1
    rgb, width, height = adapter.calls[0]
    assert (width, height) == (1001, 417) and len(rgb) == width * height * 3
    assert result["pages"][0]["raster_rgb_sha256"] == hashlib.sha256(rgb).hexdigest()
    assert observed == [True] and done == [0]
    assert all(type(value) is int and value >= 0 for value in result["timings_ns"].values())


@pytest.mark.parametrize(
    "error,reason",
    [
        (EngineError("engine_limit"), "engine_limit"),
        (RuntimeError("fictional secret directory"), "engine_recognition_failed"),
    ],
)
def test_failed_adapter_still_closes_the_actual_bitmap_and_reports_terminal_page(
    monkeypatch, error, reason
):
    allocations, done = native_spy(monkeypatch), []
    result = recognize.recognize_document(
        image_document(), "application/pdf", FakeAdapter(error), page_done=done.append
    )
    assert result["status"] == "failed" and result["reason"] == reason
    assert done == [0] and allocations[0].raw is None
    assert result["pages"][0]["reason_code"] == reason and result["parsed"]["fields"] == []
    assert "fictional secret" not in json.dumps(result)


@pytest.mark.parametrize("kind", ["text", "raster"])
def test_callback_exception_is_manual_and_done_once_after_resource_release(monkeypatch, kind):
    import pdfplumber

    closed, allocations, done = [], native_spy(monkeypatch), []
    original = pdfplumber.page.Page.close

    def close(page):
        closed.append(page.page_number)
        return original(page)

    monkeypatch.setattr(pdfplumber.page.Page, "close", close)

    def consume(*_):
        raise RuntimeError("fictional private path")

    result = prepare_pdf(
        document(TEXT) if kind == "text" else image_document(),
        _page_consumer=consume,
        _page_done=done.append,
    )
    assert (
        result["pages"][0]["route"] == "manual"
        and result["pages"][0]["reason_code"] == "preparation_failed"
    )
    assert done == [0] and "fictional private" not in json.dumps(result)
    assert (1 in closed) if kind == "text" else allocations[0].raw is None


def test_real_mixed_pdf_retains_physical_indices_and_text_then_raster_progress(monkeypatch):
    from pypdf import PdfReader, PdfWriter

    with PdfWriter() as writer, BytesIO() as output:
        for source in (document(b"UnknownFictionalOperator"), image_document(), document(TEXT)):
            with BytesIO(source) as stream:
                writer.add_page(PdfReader(stream).pages[0])
        writer.write(output)
        data = output.getvalue()
    adapter, done = FakeAdapter(), []
    result = recognize.recognize_document(data, "application/pdf", adapter, page_done=done.append)
    assert result["status"] == "manual" and sorted(done) == [0, 1, 2] and len(set(done)) == 3
    assert [page["page_index"] for page in result["pages"]] == [0, 1, 2]
    assert [page["route"] for page in result["pages"]] == ["manual", "render", "extract"]
    assert (
        len(adapter.calls) == 1
        and result["raw_text"] == "\n\nTotal: -001,234.00\n\nTotal: -001,234.00"
    )
    assert {item["page"] for item in result["parsed"]["fields"][0]["evidence"]} == {1, 2}


@pytest.mark.parametrize(
    "format,media", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")]
)
def test_actual_image_uses_one_decode_and_one_oriented_white_rgb_input(monkeypatch, format, media):
    from PIL import Image

    mode = "RGBA" if format != "JPEG" else "RGB"
    color = (10, 20, 30, 0) if mode == "RGBA" else (10, 20, 30)
    with Image.new(mode, (12, 8), color) as source, BytesIO() as output:
        exif = Image.Exif()
        exif[274] = 6
        source.save(output, format=format, exif=exif)
        data = output.getvalue()
    actual, original = [], Image.open

    def opened(*args, **kwargs):
        image = original(*args, **kwargs)
        actual.append(image)
        return image

    monkeypatch.setattr(Image, "open", opened)
    adapter, done = FakeAdapter(), []
    result = recognize.recognize_document(data, media, adapter, page_done=done.append)
    assert (
        result["status"] == "processed"
        and len(actual) == 1
        and len(adapter.calls) == 1
        and done == [0]
    )
    rgb, width, height = adapter.calls[0]
    assert (width, height) == (8, 12) and len(rgb) == width * height * 3
    if mode == "RGBA":
        assert rgb == b"\xff" * (width * height * 3)
    with pytest.raises(ValueError):
        actual[0].getpixel((0, 0))
    assert result["pages"][0]["raster_rgb_sha256"] == hashlib.sha256(rgb).hexdigest()


def test_output_budget_does_not_truncate_or_drop_completed_page_indices(monkeypatch):
    monkeypatch.setattr(recognize, "_LIMITS", replace(PrepareLimits(), dpi=300, output_bytes=128))
    result = recognize.recognize_document(document(TEXT), "application/pdf", FakeAdapter())
    assert result["status"] == "failed" and result["reason"] == "engine_limit"
    assert result["pages"][0]["page_index"] == 0 and result["pages"][0]["text"] == ""
    assert result["raw_text"] == "" and result["parsed"]["fields"] == []
