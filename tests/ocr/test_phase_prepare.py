"""Actual native preparation clocks; the fictional adapter is never a performance claim."""

import hashlib
import json
from io import BytesIO

import pytest
from coinpup_api.ocr import recognize
from coinpup_api.ocr.engine_adapters import EngineError
from coinpup_api.ocr.parser_types import TextPage, Word
from coinpup_api.ocr.pdf_prepare import PrepareLimits, prepare_pdf

from scripts.ocr_benchmark.phase_clock import PhaseJournal
from tests.ocr.pdf_fixtures import document, image_document

pytestmark = pytest.mark.ocr
TEXT = b"BT /F1 12 Tf 10 60 Td (Total: -001,234.00) Tj ET"


class FictionalAdapter:
    def __init__(self, callback=None):
        self.callback, self.calls = callback, []

    def recognize(self, pixels, width, height):
        self.calls.append((pixels, width, height))
        if self.callback is not None:
            self.callback(pixels, width, height)
        return TextPage(
            width, height, (Word("Total: -001,234.00", (0, 0, width, 10)),), "Total: -001,234.00"
        )


def png():
    from PIL import Image

    with Image.new("RGBA", (12, 8), (10, 20, 30, 0)) as image, BytesIO() as stream:
        exif = Image.Exif()
        exif[274] = 6
        image.save(stream, format="PNG", exif=exif)
        return stream.getvalue()


def non_time(result):
    return {key: value for key, value in result.items() if key != "timings_ns"}


@pytest.mark.parametrize("kind", ["text", "pdf_raster", "image"])
def test_instrumentation_preserves_every_non_time_output_and_only_real_phases(tmp_path, kind):
    data = document(TEXT) if kind == "text" else image_document() if kind == "pdf_raster" else png()
    media = "image/png" if kind == "image" else "application/pdf"
    expected = recognize.recognize_document(data, media, FictionalAdapter())
    with PhaseJournal(tmp_path / "phases.jsonl") as clock:
        actual = recognize.recognize_document(
            data, media, FictionalAdapter(), _phase_observer=clock
        )
        assert non_time(actual) == non_time(expected)
        phases = [event["phase"] for event in clock.events]
        if kind == "text":
            assert phases == ["prepare_total", "prepare_total", "parse", "parse"]
        else:
            assert phases == [
                "prepare_total",
                "image_decode" if kind == "image" else "pdf_render",
                "image_decode" if kind == "image" else "pdf_render",
                "rgb_materialize",
                "rgb_materialize",
                "sdk_recognize",
                "sdk_recognize",
                "prepare_total",
                "parse",
                "parse",
            ]
        ticks = [event["at_ns"] for event in clock.events]
        assert all(type(tick) is int and tick > 0 for tick in ticks) and ticks == sorted(ticks)
        assert all(
            event["details"]
            == (
                {"outcome": "passed", "bitmap_allocation_attempted": True}
                if event["phase"] == "pdf_render"
                else {"outcome": "passed"}
            )
            for event in clock.events
            if event["edge"] == "end"
        )


def test_exact_native_render_boundary_and_sdk_start_are_live_and_durable(tmp_path, monkeypatch):
    import pypdfium2 as pdfium

    path, allocated, renders = tmp_path / "phases.jsonl", [], []
    original_render, original_allocate = pdfium.PdfPage.render, pdfium.PdfBitmap.new_native
    with PhaseJournal(path) as clock:

        def allocate(*args, **kwargs):
            bitmap = original_allocate(*args, **kwargs)
            allocated.append(bitmap)
            return bitmap

        def render(page, *args, **kwargs):
            assert clock.events[-1]["phase"] == "pdf_render" and clock.events[-1]["edge"] == "start"
            renders.append(kwargs.copy())
            result = original_render(page, *args, **kwargs)
            assert clock.events[-1]["edge"] == "start"
            return result

        monkeypatch.setattr(pdfium.PdfBitmap, "new_native", allocate)
        monkeypatch.setattr(pdfium.PdfPage, "render", render)

        def sdk(pixels, width, height):
            assert allocated[0].raw is not None
            records = list(map(json.loads, path.read_bytes().splitlines()))
            assert records == clock.events
            assert records[-1]["phase"] == "sdk_recognize" and records[-1]["edge"] == "start"
            assert records[-1]["details"] == {
                "raster_rgb_sha256": hashlib.sha256(pixels).hexdigest(),
                "width": width,
                "height": height,
            }
            render_events = [item for item in records if item["phase"] == "pdf_render"]
            assert [item["edge"] for item in render_events] == ["start", "end"]

        def done(index):
            assert index == 0 and allocated[0].raw is None

        result = recognize.recognize_document(
            image_document(),
            "application/pdf",
            FictionalAdapter(sdk),
            page_done=done,
            _phase_observer=clock,
        )
    assert result["status"] == "processed" and len(renders) == len(allocated) == 1
    assert renders[0]["rotation"] == 0 and renders[0]["scale"] == 300 / 72
    assert allocated[0].raw is None


@pytest.mark.parametrize("error", [EngineError("engine_limit"), RuntimeError("fictional secret")])
def test_real_image_decode_ends_before_sdk_and_failure_closes_resources(
    tmp_path, monkeypatch, error
):
    from PIL import Image

    data, opened = png(), []
    original = Image.open

    def open_image(*args, **kwargs):
        image = original(*args, **kwargs)
        opened.append(image)
        return image

    monkeypatch.setattr(Image, "open", open_image)
    with PhaseJournal(tmp_path / "phases.jsonl") as clock:

        def sdk(pixels, width, height):
            assert (width, height) == (8, 12) and pixels == b"\xff" * (width * height * 3)
            assert [
                event["edge"] for event in clock.events if event["phase"] == "image_decode"
            ] == ["start", "end"]
            raise error

        result = recognize.recognize_document(
            data, "image/png", FictionalAdapter(sdk), _phase_observer=clock
        )
        ends = [
            event
            for event in clock.events
            if event["phase"] == "sdk_recognize" and event["edge"] == "end"
        ]
        assert len(ends) == 1 and ends[0]["details"] == {"outcome": "failed"}
    assert len(opened) == 1 and result["status"] == "failed"
    assert "fictional secret" not in json.dumps(result)
    with pytest.raises(ValueError):
        opened[0].getpixel((0, 0))


@pytest.mark.parametrize("contents", [b"UnknownFictionalOperator", b"BT /F1 12 Tf () Tj ET"])
def test_manual_pages_have_no_fabricated_render_or_sdk_interval(tmp_path, contents):
    adapter = FictionalAdapter()
    with PhaseJournal(tmp_path / "phases.jsonl") as clock:
        result = recognize.recognize_document(
            document(contents), "application/pdf", adapter, _phase_observer=clock
        )
        assert {event["phase"] for event in clock.events} == {"prepare_total", "parse"}
    assert result["status"] == "manual" and adapter.calls == []


def test_real_native_render_failure_has_failed_end_and_releases_allocated_bitmap(
    tmp_path, monkeypatch
):
    import pypdfium2 as pdfium

    allocated, original_render, original_allocate = (
        [],
        pdfium.PdfPage.render,
        pdfium.PdfBitmap.new_native,
    )

    def allocate(*args, **kwargs):
        bitmap = original_allocate(*args, **kwargs)
        allocated.append(bitmap)
        return bitmap

    def broken_render(*args, **kwargs):
        original_render(*args, **kwargs)
        raise RuntimeError("fictional native failure")

    monkeypatch.setattr(pdfium.PdfBitmap, "new_native", allocate)
    monkeypatch.setattr(pdfium.PdfPage, "render", broken_render)
    with PhaseJournal(tmp_path / "phases.jsonl") as clock:
        actual = prepare_pdf(image_document(), PrepareLimits(dpi=300), _phase_observer=clock)
        assert clock.events[-1]["phase"] == "pdf_render"
        assert clock.events[-1]["details"] == {
            "outcome": "failed",
            "bitmap_allocation_attempted": True,
        }
    assert actual["pages"][0]["route"] == "manual"
    assert len(allocated) == 1 and allocated[0].raw is None
