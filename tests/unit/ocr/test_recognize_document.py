"""Shared parser routing using handwritten plain inputs, without optional native libraries."""

from types import SimpleNamespace

import pytest
from coinpup_api.ocr import recognize
from coinpup_api.ocr.field_parser import parse_document
from coinpup_api.ocr.parser_types import TextPage, Word


def text_page(text="Total: -001,234.00"):
    return TextPage(400, 200, (Word(text, (10, 10, 200, 30)),), text)


@pytest.mark.parametrize(
    "data,media",
    [(b"", "application/pdf"), (bytearray(b"x"), "application/pdf"), (b"x", "image/svg+xml")],
)
def test_invalid_original_is_rejected_before_preparation(monkeypatch, data, media):
    calls = []
    monkeypatch.setattr(recognize, "prepare_pdf", lambda *args, **kwargs: calls.append(True))
    result = recognize.recognize_document(data, media, None)
    assert result["status"] == "failed" and result["reason"] == "request_invalid"
    assert calls == [] and result["pages"] == []


def test_mixed_manual_page_preserves_later_good_page_and_physical_evidence(monkeypatch):
    actual, seen = text_page(), []

    def prepare(data, limits, *, _page_consumer, _page_done):
        assert limits.dpi == 300
        _page_consumer(1, actual)
        _page_done(1)
        _page_done(0)
        return {
            "reason_code": None,
            "pages": [
                {
                    "page_index": 0,
                    "layer": "unknown",
                    "route": "manual",
                    "reason_code": "unsupported_pdf",
                },
                {"page_index": 1, "layer": "present", "route": "extract", "reason_code": None},
            ],
        }

    monkeypatch.setattr(recognize, "prepare_pdf", prepare)
    adapter = SimpleNamespace(
        recognize=lambda *_: pytest.fail("Text/manual must never use the engine")
    )
    result = recognize.recognize_document(
        b"fictional", "application/pdf", adapter, page_done=seen.append
    )
    assert result["status"] == "manual" and result["reason"] == "unsupported_pdf"
    assert seen == [1, 0] and [page["page_index"] for page in result["pages"]] == [0, 1]
    assert result["raw_text"] == "\n\n" + actual.text
    assert (
        result["pages"][1]["route"] == "extract" and result["pages"][1]["geometry"]["units"] == "pt"
    )
    field = result["parsed"]["fields"][0]
    assert field["value"] == "-1234.00" and field["evidence"][0]["page"] == 1


def test_parser_review_remains_processed_and_original_parser_result_is_unmodified(monkeypatch):
    actual = text_page("Currency: $")

    def prepare(*args, _page_consumer, _page_done):
        _page_consumer(0, actual)
        _page_done(0)
        return {
            "reason_code": None,
            "pages": [
                {"page_index": 0, "layer": "present", "route": "extract", "reason_code": None}
            ],
        }

    monkeypatch.setattr(recognize, "prepare_pdf", prepare)
    result = recognize.recognize_document(b"fictional", "application/pdf", None)
    assert result["status"] == "processed" and result["reason"] is None
    assert result["parsed"] == parse_document((actual,))
    assert result["parsed"]["fields"][0]["reason"] == "unknown_currency"
    assert result["timings_ns"]["recognize"] == 0
    assert all(type(value) is int and value >= 0 for value in result["timings_ns"].values())


def test_final_document_budget_failure_retains_progress_slots_without_extracted_answers(
    monkeypatch,
):
    def prepare(*args, _page_consumer, _page_done):
        _page_consumer(0, text_page())
        _page_done(0)
        return {"reason_code": "prepare_limit", "pages": []}

    monkeypatch.setattr(recognize, "prepare_pdf", prepare)
    result = recognize.recognize_document(b"fictional", "application/pdf", None)
    assert result["status"] == "manual" and result["reason"] == "prepare_limit"
    assert result["raw_text"] == "" and result["parsed"]["fields"] == []
    assert result["pages"][0]["page_index"] == 0 and result["pages"][0]["route"] == "manual"
