"""Actual fictional PDF rendering distinguishes calls from attempted allocation."""

from dataclasses import replace

import pypdfium2 as pdfium
import pytest
from coinpup_api.ocr.pdf_prepare import PrepareLimits, rendered_page
from coinpup_api.ocr.pdf_probe import _Uncertain

from tests.ocr.pdf_fixtures import image_document


def observer_events():
    events = []

    def observe(phase, page_index, edge, at_ns, details):
        events.append(
            {
                "phase": phase,
                "page_index": page_index,
                "edge": edge,
                "at_ns": at_ns,
                "details": dict(details or {}),
            }
        )

    return events, observe


@pytest.mark.parametrize("limit", ["max_side", "page_pixels", "bitmap_bytes", "document_pixels"])
def test_actual_budget_rejection_is_observed_before_any_native_allocation(monkeypatch, limit):
    events, observe = observer_events()
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("pixel budget must reject before allocating")

    monkeypatch.setattr(pdfium.PdfBitmap, "new_native", forbidden)
    limits = replace(PrepareLimits(), dpi=72, **{limit: 1})
    with pdfium.PdfDocument(image_document()) as document:
        with pytest.raises(_Uncertain) as rejected:
            with rendered_page(document, 0, limits, _phase_observer=observe):
                pytest.fail("rejected page must not yield a bitmap")
        assert rejected.value.reason == "prepare_limit"
    assert calls == []
    assert [(event["phase"], event["edge"]) for event in events] == [
        ("pdf_render", "start"),
        ("pdf_render", "end"),
    ]
    assert events[-1]["details"] == {
        "bitmap_allocation_attempted": False,
        "outcome": "failed",
    }


@pytest.mark.parametrize("error", [RuntimeError, MemoryError])
def test_native_allocation_failure_cannot_claim_preallocation_rejection(monkeypatch, error):
    events, observe = observer_events()
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise error("fictional native allocation failure")

    monkeypatch.setattr(pdfium.PdfBitmap, "new_native", fail)
    with pdfium.PdfDocument(image_document()) as document:
        with pytest.raises(error, match="fictional native allocation failure"):
            with rendered_page(
                document, 0, replace(PrepareLimits(), dpi=72), _phase_observer=observe
            ):
                pytest.fail("failed allocation must not yield")
    assert len(calls) == 1
    assert events[-1]["details"] == {
        "bitmap_allocation_attempted": True,
        "outcome": "failed",
    }


def test_successful_real_bitmap_and_consumer_failure_keep_attempt_evidence():
    events, observe = observer_events()
    with pdfium.PdfDocument(image_document()) as document:
        with pytest.raises(RuntimeError, match="fictional consumer failure"):
            with rendered_page(
                document, 0, replace(PrepareLimits(), dpi=72), _phase_observer=observe
            ) as bitmap:
                assert bitmap.raw and (bitmap.width, bitmap.height) == (240, 100)
                assert events[-1]["details"] == {
                    "bitmap_allocation_attempted": True,
                    "outcome": "passed",
                }
                raise RuntimeError("fictional consumer failure")
    assert events[-1]["details"]["bitmap_allocation_attempted"] is True
