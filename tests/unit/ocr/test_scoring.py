"""Hand-authored expected outcomes exercise frozen scoring, without an OCR/parser oracle."""

import importlib.util
import sys
from copy import deepcopy
from fractions import Fraction
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/ocr_benchmark/scoring.py"
SPEC = importlib.util.spec_from_file_location("coinpup_ocr_scoring", SCRIPT)
assert SPEC and SPEC.loader
scoring = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = scoring
SPEC.loader.exec_module(scoring)
FieldScore = scoring.FieldScore


def field(path, value, status="certain"):
    return {"path": path, "value": value, "status": status}


def test_missing_wrong_review_and_extra_each_keep_the_frozen_denominator():
    expected = {"header.total": "-12.30", "header.date": "2026-10-04", "header.currency": "USD"}
    predictions = [
        field("header.total", "-12.30"),
        field("header.date", "2026-10-05"),
        field("header.currency", "USD", "review"),
        field("header.tax", "0.00"),
    ]
    score = scoring.score_fields(expected, predictions)
    assert score == FieldScore(1, 3, 1)
    assert score.incorrect == 2 and score.denominator == 4 and score.accuracy == Fraction(1, 4)
    assert scoring.score_fields(expected, []) == FieldScore(0, 3, 0)


@pytest.mark.parametrize("values", [("10", "10"), ("wrong", "10"), ("10", "wrong")])
def test_duplicate_paths_are_wrong_and_extra_without_choosing_the_best_answer(values):
    score = scoring.score_fields(
        {"header.total": "10"}, [field("header.total", value) for value in values]
    )
    assert score == FieldScore(0, 1, 1)


def test_all_unknown_occurrences_count_and_input_records_are_not_mutated():
    expected = {"header.total": "10"}
    predicted = [field("header.total", "10"), field("header.tax", "0"), field("header.tax", "0")]
    before = deepcopy((expected, predicted))
    assert scoring.score_fields(expected, predicted) == FieldScore(1, 1, 2)
    assert (expected, predicted) == before


@pytest.mark.parametrize("value", ["12.3", "12.30", "+12.30", "-12.300", " -12.30"])
def test_scorer_does_not_normalize_money_or_correct_an_ocr_token(value):
    assert scoring.score_fields(
        {"header.total": "-12.30"}, [field("header.total", value)]
    ) == FieldScore(0, 1, 0)


def test_repeated_amounts_in_distinct_rows_are_not_set_deduplicated():
    expected = {"rows.0.0.0.amount": "10", "rows.0.0.1.amount": "10", "rows.1.0.0.amount": "10"}
    assert scoring.score_fields(
        expected, [field(path, value) for path, value in expected.items()]
    ) == FieldScore(3, 3, 0)


def test_rows_are_matched_by_printed_ordinal_not_nearest_truth_value():
    expected = {"rows.0.0.0.amount": "10", "rows.0.0.1.amount": "20", "rows.0.0.2.amount": "30"}
    missing_first = [field("rows.0.0.0.amount", "20"), field("rows.0.0.1.amount", "30")]
    inserted_first = [
        field(f"rows.0.0.{index}.amount", value)
        for index, value in enumerate(["99", "10", "20", "30"])
    ]
    assert scoring.score_fields(expected, missing_first) == FieldScore(0, 3, 0)
    assert scoring.score_fields(expected, inserted_first) == FieldScore(0, 3, 1)
    assert scoring.score_fields(expected, [field("rows.0.0.2.amount", "30")]) == FieldScore(1, 3, 0)


@pytest.mark.parametrize(
    "path",
    [
        "rows.00.0.0.amount",
        "rows.-1.0.0.amount",
        "rows.0.0.amount",
        "header.Total",
        "header.total\n",
    ],
)
def test_noncanonical_paths_cannot_be_normalized_into_a_match(path):
    with pytest.raises(ValueError, match="Invalid predicted fields"):
        scoring.score_fields({"header.total": "10"}, [field(path, "10")])


@pytest.mark.parametrize(
    "record",
    [
        {"path": "header.total", "value": 10.0, "status": "certain"},
        {"path": "header.total", "value": "10", "status": "certain", "sample_id": "fictional"},
        {"path": "header.total", "value": "10", "status": "confirmed"},
        {"path": "header.total", "value": "10"},
    ],
)
def test_only_exact_record_schema_is_accepted(record):
    with pytest.raises(ValueError):
        scoring.score_fields({"header.total": "10"}, [record])


def test_null_and_review_values_are_incorrect_and_empty_truth_cannot_qualify():
    assert scoring.score_fields(
        {"header.total": "10"}, [field("header.total", None)]
    ) == FieldScore(0, 1, 0)
    assert scoring.score_fields({}, [field("header.total", "10")]) == FieldScore(0, 0, 1)
    empty = scoring.score_fields({}, [])
    assert empty.accuracy is None and not scoring.passes(empty) and not scoring.passes(empty, 100)


def test_corpus_aggregation_adds_counts_not_document_percentages():
    aggregate = scoring.aggregate_scores([FieldScore(1, 1, 0), FieldScore(0, 99, 1)])
    assert aggregate == FieldScore(1, 100, 1) and aggregate.accuracy == Fraction(1, 101)


def test_95_percent_uses_exact_counts_at_and_infinitesimally_below_the_gate():
    assert scoring.passes(FieldScore(19, 20, 0))
    assert not scoring.passes(FieldScore(19, 20, 1))
    assert scoring.passes(FieldScore(95 * 10**30, 100 * 10**30, 0))
    assert not scoring.passes(FieldScore(95 * 10**30 - 1, 100 * 10**30, 0))


def test_two_points_boundary_accounts_for_different_and_extra_denominators():
    assert scoring.within_two_points(FieldScore(49, 50, 0), FieldScore(96, 100, 0))
    assert not scoring.within_two_points(FieldScore(98, 100, 1), FieldScore(95, 100, 0))
    assert scoring.within_two_points(
        FieldScore(97 * 10**30, 100 * 10**30, 0), FieldScore(95 * 10**30, 100 * 10**30, 0)
    )
    assert not scoring.within_two_points(
        FieldScore(97 * 10**30 + 1, 100 * 10**30, 0), FieldScore(95 * 10**30, 100 * 10**30, 0)
    )


@pytest.mark.parametrize(
    "paddle,tesseract,selected,reason",
    [
        (FieldScore(94, 100, 0), FieldScore(94, 100, 0), None, "neither_qualified"),
        (FieldScore(95, 100, 0), FieldScore(94, 100, 0), "paddle", "paddle_only"),
        (FieldScore(94, 100, 0), FieldScore(95, 100, 0), "tesseract", "tesseract_only"),
        (FieldScore(98, 100, 0), FieldScore(96, 100, 0), "tesseract", "within_two_points"),
        (FieldScore(99, 100, 0), FieldScore(95, 100, 0), "paddle", "higher_accuracy"),
        (FieldScore(95, 100, 0), FieldScore(99, 100, 0), "tesseract", "higher_accuracy"),
    ],
)
def test_engine_selection_only_uses_qualified_frozen_candidates(
    paddle, tesseract, selected, reason
):
    result = scoring.select_engine(FieldScore(100, 100, 0), paddle, tesseract)
    assert (
        result.selected == selected
        and result.reason == reason
        and result.stop == (selected is None)
    )


@pytest.mark.parametrize(
    "text", [FieldScore(99, 100, 0), FieldScore(100, 100, 1), FieldScore(0, 0, 0)]
)
def test_text_100_percent_is_an_independent_blocking_gate(text):
    result = scoring.select_engine(text, FieldScore(100, 100, 0), FieldScore(100, 100, 0))
    assert result == scoring.Selection(None, True, "text_gate_failed")


@pytest.mark.parametrize(
    "reference,prediction,edits,chars",
    [
        ("café\r\nUSD", "cafe\u0301\nUSD", 0, 8),
        ("USD  10.00", "usd 10,00", 5, 10),
        ("A\rB", "A\nB", 1, 3),
        ("a", "abc", 2, 1),
        ("", "fictional", 9, 0),
        ("", "", 0, 0),
        ("中文", "中", 1, 2),
        ("ab", "ba", 2, 2),
    ],
)
def test_unicode_cer_preserves_case_punctuation_and_whitespace(reference, prediction, edits, chars):
    assert scoring.character_errors(reference, prediction) == scoring.CharacterScore(edits, chars)


def test_cer_micro_macro_and_empty_reference_false_positives_are_separate():
    report = scoring.aggregate_cer(
        [
            scoring.character_errors("a", "abc"),
            scoring.character_errors("1234", "1234"),
            scoring.character_errors("", "x"),
            scoring.character_errors("", ""),
        ]
    )
    assert report == {
        "documents": 4,
        "edits": 3,
        "reference_chars": 5,
        "micro": Fraction(3, 5),
        "macro": Fraction(1, 1),
        "empty_reference_documents": 2,
        "false_positive_documents": 1,
        "false_positive_chars": 1,
    }
    assert scoring.aggregate_cer([scoring.character_errors("", "x")])["micro"] is None


@pytest.mark.parametrize("score", [(-1, 1, 0), (2, 1, 0), (True, 1, 0), (0, 1, -1)])
def test_invalid_count_records_cannot_fabricate_a_gate(score):
    with pytest.raises(ValueError):
        FieldScore(*score)
