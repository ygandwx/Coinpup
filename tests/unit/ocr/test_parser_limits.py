"""Whole-result rejection protects untrusted words, geometry and cumulative budgets."""

import json
import tracemalloc
from dataclasses import replace

import pytest
from coinpup_api.ocr.field_parser import parse_document
from coinpup_api.ocr.parser_types import ParserLimits, TextPage, Word

from tests.unit.ocr.test_field_parser import page, table_words


def rejected(result, reason):
    assert result == {
        "version": 1,
        "status": "review",
        "reason": reason,
        "fields": [],
        "rows": [],
        "diagnostics": [reason],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"max_pages": 51},
        {"max_words": 0},
        {"text_bytes": True},
        {"output_bytes": 127},
        {"word_bytes": -1},
    ],
)
def test_limits_only_allow_explicit_positive_integers_at_or_below_frozen_caps(change):
    with pytest.raises(ValueError, match="Invalid parser limits"):
        ParserLimits(**change)


@pytest.mark.parametrize(
    "bbox",
    [
        (1, 2, float("nan"), 20),
        (1, 2, float("inf"), 20),
        (False, 2, 5, 20),
        (10, 2, 5, 20),
        (-1, 2, 5, 20),
        (1, 2, 901, 20),
    ],
)
def test_invalid_nonfinite_or_outside_word_coordinates_cannot_produce_partial_fields(bbox):
    rejected(parse_document((TextPage(900, 1400, (Word("Total: 1.00", bbox),)),)), "invalid_input")


@pytest.mark.parametrize(
    "change",
    [
        {"width": float("nan")},
        {"height": float("inf")},
        {"width": True},
        {"height": 0},
        {"text": "fictional\ud800"},
    ],
)
def test_invalid_page_shapes_and_unicode_fail_without_echoing_source(change):
    rejected(parse_document((replace(page(["Total: 1.00"]), **change),)), "invalid_input")


@pytest.mark.parametrize("text", ["Total:\x00 1.00", "Total:\n1.00", "   "])
def test_invalid_word_text_is_not_silently_stripped_into_a_valid_amount(text):
    rejected(parse_document((TextPage(100, 100, (Word(text, (1, 1, 80, 12)),)),)), "invalid_input")


@pytest.mark.parametrize(
    "limit,value",
    [
        ("words_per_page", 1),
        ("max_words", 1),
        ("max_lines", 1),
        ("max_fields", 1),
        ("evidence_bytes", 1),
        ("output_bytes", 128),
    ],
)
def test_exhaustion_returns_one_complete_failure_not_a_truncated_candidate(limit, value):
    result = parse_document(
        (page(["Currency: GBP", "Total: 1.00"]),), limits=replace(ParserLimits(), **{limit: value})
    )
    rejected(result, "parser_limit")
    if limit == "output_bytes":
        assert len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf8")) <= 128


@pytest.mark.parametrize("limit", ["max_pages", "max_words", "text_bytes"])
def test_whole_document_budget_does_not_reset_between_pages(limit):
    source = page(["Total: 1.00"])
    bound = len(b"Total: 1.00") if limit == "text_bytes" else 1
    limits = replace(ParserLimits(), **{limit: bound})
    assert parse_document((source,), limits=limits)["fields"]
    rejected(parse_document((source, source), limits=limits), "parser_limit")


def test_utf8_budget_counts_actual_bytes_not_only_character_count():
    source = page(["合计: 1.00"])
    assert parse_document((source,), limits=ParserLimits(text_bytes=12))["fields"]
    rejected(parse_document((source,), limits=ParserLimits(text_bytes=10)), "parser_limit")
    rejected(parse_document((page(["Total: 1.00"], text="x" * 131073),)), "parser_limit")


def test_per_word_budget_is_checked_before_field_extraction():
    rejected(
        parse_document((page(["Total: 1.00"]),), limits=ParserLimits(word_bytes=3)), "parser_limit"
    )


def test_oversized_text_is_rejected_before_materializing_its_utf8_copy():
    source = page(text="€" * 200000)
    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        tracemalloc.reset_peak()
        rejected(parse_document((source,), limits=ParserLimits(text_bytes=8)), "parser_limit")
        assert tracemalloc.get_traced_memory()[1] - before < 128 * 1024
    finally:
        if not was_tracing:
            tracemalloc.stop()


def test_minimum_output_budget_fits_complete_invalid_input_and_layout_errors():
    examples = [
        TextPage(100, 100, (Word("Total: 1.00", (1, 1, float("nan"), 12)),)),
        TextPage(
            200,
            100,
            (Word("Total: 1.00", (10, 10, 110, 22)), Word("Total: 2.00", (50, 10, 150, 22))),
        ),
        page(["第 2 页 / Page 1 of 2"]),
    ]
    for source in examples:
        result = parse_document((source,), limits=ParserLimits(output_bytes=128))
        assert result["status"] == "review" and result["fields"] == [] and result["rows"] == []
        assert len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf8")) <= 128
    assert result["reason"] == "parser_limit"


def test_rows_and_independent_tables_have_explicit_budgets():
    first = table_words(
        [("2031-01-01", "EUR", "1.00", "Fictional A"), ("2031-01-02", "EUR", "2.00", "Fictional B")]
    )
    rejected(parse_document((page(extra=first),), limits=ParserLimits(max_rows=1)), "parser_limit")
    second = table_words([("2031-02-01", "EUR", "3.00", "Fictional C")], y=500)
    rejected(
        parse_document((page(extra=first + second),), limits=ParserLimits(max_tables=1)),
        "parser_limit",
    )


def test_overlapping_words_do_not_choose_the_amount_that_matches_a_possible_answer():
    source = TextPage(
        200, 100, (Word("Total: 1.00", (10, 10, 110, 22)), Word("Total: 2.00", (50, 10, 150, 22)))
    )
    rejected(parse_document((source,)), "ambiguous_layout")
