"""Independent fictional report inputs; no frozen corpus answers or SDK execution."""

import json
from copy import deepcopy
from fractions import Fraction

import pytest

from scripts.ocr_benchmark.formal_report import (
    FormalReportError,
    diagnose_predictions,
    nearest_rank,
    project_result,
    summarize_candidate,
    summarize_document,
    summarize_latencies,
    validate_repeats,
)


def field(path, value, status="certain"):
    return {"path": path, "value": value, "status": status, "evidence": [{"raw": "FICTIONAL"}]}


def case(identifier="fictional-a", group="ocr", expected=None, reference="abc"):
    return {
        "id": identifier,
        "group": group,
        "expected_fields": {"header.total": "12.00"} if expected is None else expected,
        "reference_text": reference,
    }


def record(identifier="fictional-a", repeat=0, fields=None, text="abc", status="processed"):
    return {
        "case_id": identifier,
        "round": repeat,
        "submitted_ns": 1000,
        "returned_ns": 1000 + (repeat + 1) * 100,
        "output": {
            "status": status,
            "reason": None,
            "parsed": {
                "status": "parsed",
                "reason": None,
                "diagnostics": [],
                "fields": [field("header.total", "12.00")] if fields is None else fields,
                "rows": [],
            },
            "pages": [{"page_index": 0, "route": "render", "reason_code": None}],
            "raw_text": text,
            "timings_ns": {
                "prepare": repeat + 1,
                "recognize": repeat + 2,
                "parse": repeat + 3,
                "source": {"start": 10, "end": 15},
            },
        },
    }


@pytest.mark.parametrize(
    "reason",
    [
        "probe_limit",
        "prepare_limit",
        "parser_limit",
        "engine_limit",
        "resource_limit",
    ],
)
@pytest.mark.parametrize("location", ["root", "page", "parsed", "diagnostic", "field"])
def test_all_explicit_budget_failures_score_empty_without_erasing_evidence(reason, location):
    actual = record()
    output = actual["output"]
    if location == "root":
        output["reason"] = reason
    elif location == "page":
        output["pages"][0]["reason_code"] = reason
    elif location == "parsed":
        output["parsed"]["reason"] = reason
    elif location == "diagnostic":
        output["parsed"]["diagnostics"] = [reason]
    else:
        output["parsed"]["fields"][0]["reason"] = reason
    original = deepcopy(actual)
    assert project_result(actual) == ([], "")
    assert actual == original
    score = summarize_document(case(), actual)
    assert (score["C"], score["T"], score["E"]) == (0, 1, 0)
    assert score["cer"]["edits"] == 3


@pytest.mark.parametrize("status", ["failed", "timeout"])
def test_terminal_failure_keeps_denominator_even_with_partial_success(status):
    result = summarize_document(case(), record(status=status))
    assert (result["C"], result["T"], result["E"]) == (0, 1, 0)
    assert result["missing"] == ["header.total"] and result["cer"]["edits"] == 3


def test_manual_mixed_pages_and_review_fields_keep_actual_usable_predictions():
    expected = {"header.total": "12.00", "header.currency": "USD"}
    actual = record(
        fields=[field("header.total", "12.00"), field("header.currency", None, "review")],
        status="manual",
    )
    actual["output"]["reason"] = "unusable_text"
    actual["output"]["parsed"].update(status="review", reason="unknown_currency")
    actual["output"]["pages"].append(
        {"page_index": 1, "route": "manual", "reason_code": "unusable_text"}
    )
    result = summarize_document(case(expected=expected), actual)
    assert (result["C"], result["T"], result["E"], result["review"]) == (1, 2, 0, 1)
    assert result["cer"]["edits"] == 0
    actual["output"]["raw_text"] = "parser_limit"
    assert project_result(actual)[1] == "parser_limit"  # literal text is never a reason


def test_duplicate_extra_and_missing_fields_are_never_deduplicated():
    fields = [
        field("header.total", "12.00"),
        field("header.total", "12.00"),
        field("header.tax", "2.00"),
        field("header.tax", "2.00"),
    ]
    result = summarize_document(
        case(expected={"header.total": "12.00", "header.currency": "USD"}), record(fields=fields)
    )
    assert (result["C"], result["T"], result["E"]) == (0, 2, 3)
    assert result["missing"] == ["header.currency"]
    assert result["duplicates"] == {"header.tax": 2, "header.total": 2}
    assert result["extra_paths"] == {"header.tax": 2}


def test_table_rows_use_fixed_identity_and_distinguish_complete_from_correct():
    expected = {
        f"rows.0.0.{row}.{role}": value
        for row in (0, 1, 2)
        for role, value in {"date": "2039-01-01", "currency": "USD", "amount": "5.00"}.items()
    }
    fields = [
        field(path, "6.00" if path.endswith("1.amount") else value)
        for path, value in expected.items()
        if ".2." not in path
    ]
    fields.append(field("rows.0.1.0.amount", "5.00"))
    result = summarize_document(case(expected=expected), record(fields=fields))
    assert result["tables"] == [
        {
            "page": 0,
            "table": 0,
            "expected_rows": 3,
            "complete_rows": 2,
            "correct_rows": 1,
            "missing_rows": 1,
            "extra_rows": 0,
            "complete_ratio": {"numerator": 2, "denominator": 3},
        },
        {
            "page": 0,
            "table": 1,
            "expected_rows": 0,
            "complete_rows": 0,
            "correct_rows": 0,
            "missing_rows": 0,
            "extra_rows": 1,
            "complete_ratio": None,
        },
    ]
    fields.append(field("rows.0.0.0.amount", "5.00"))
    changed = summarize_document(case(expected=expected), record(fields=fields))
    assert changed["tables"][0]["complete_rows"] == 1
    assert changed["tables"][0]["correct_rows"] == 0


def test_arithmetic_is_exact_for_negative_and_large_decimals_and_never_repairs():
    fields = [
        field("header.subtotal", "99999999999999999999.99"),
        field("header.tax", "-0.09"),
        field("header.total", "99999999999999999999.90"),
        field("header.opening_balance_usd", "100.00"),
        field("header.closing_balance_usd", "99.91"),
        field("rows.0.0.0.currency", "USD"),
        field("rows.0.0.0.amount", "-0.09"),
        field("header.opening_balance_eur", "1.00"),
        field("header.closing_balance_eur", "3.00"),
        field("rows.0.0.1.currency", "EUR"),
        field("rows.0.0.1.amount", "2.00"),
    ]
    original = deepcopy(fields)
    result = diagnose_predictions(fields)
    assert result["subtotal_plus_tax"] == "consistent"
    assert [item["status"] for item in result["balances"]] == ["consistent", "consistent"]
    assert fields == original and "cannot be inferred" in result["scope"]
    fields[2]["value"] = "0.00"
    assert diagnose_predictions(fields)["subtotal_plus_tax"] == "mismatch"
    fields[1]["status"] = "review"
    fields[-1]["status"] = "review"
    result = diagnose_predictions(fields)
    assert result["subtotal_plus_tax"] == "not_checkable"
    assert all(item["status"] == "not_checkable" for item in result["balances"])


def test_nearest_rank_uses_integer_ceiling_including_zero_without_interpolation():
    assert nearest_rank([], Fraction(1, 2)) is None
    assert nearest_rank([0], Fraction(95, 100)) == 0
    assert nearest_rank([30, 10, 20, 40], Fraction(1, 2)) == 20
    assert nearest_rank([30, 10, 20, 40], Fraction(95, 100)) == 40
    assert nearest_rank(list(range(120)), Fraction(95, 100)) == 113


@pytest.mark.parametrize(
    "samples,p",
    [
        ([True], Fraction(1, 2)),
        ([-1], Fraction(1, 2)),
        ([1.5], Fraction(1, 2)),
        ([1], 0.5),
        ([1], Fraction(0)),
        ([1], Fraction(2)),
    ],
)
def test_invalid_latency_data_is_not_coerced(samples, p):
    with pytest.raises(FormalReportError):
        nearest_rank(samples, p)


def test_failures_are_in_latency_denominator_but_placeholder_phases_are_excluded():
    good, bad = record(), record(repeat=1, status="failed")
    bad["returned_ns"] = bad["submitted_ns"] + 60_000_000_000
    bad["output"]["timings_ns"].update(prepare=0, recognize=0, parse=0)
    result = summarize_latencies([good, bad])
    assert result["all"] == {"N": 2, "p50_ns": 100, "p95_ns": 60_000_000_000}
    assert result["success"]["N"] == 1 and result["phases"]["recognize"]["N"] == 1
    assert result["phases"]["source"]["N"] == 2
    assert "not pure rendering" in result["prepare_scope"]


def test_five_rounds_score_first_only_sum_counts_and_serialize_exact_cer():
    cases = [case(), case("fictional-b", expected={"header.total": "12.00", "header.tax": "1.00"})]
    records = [record(item["id"], repeat) for item in cases for repeat in range(5)]
    cases += [case("fictional-text", "text"), case("fictional-empty", "error", {}, "")]
    records += [record("fictional-text"), record("fictional-empty", fields=[], text="x")]
    cold = [{"started_ns": 5, "ready_ns": 15, "record": record()}]
    resources = [{"same_window_rss_peak_bytes": 123, "individual_hwm_bytes": [100, 50]}]
    result = summarize_candidate(cases, records, cold_records=cold, resource_reports=resources)
    assert result["groups"]["ocr"]["accuracy"] == {"numerator": 2, "denominator": 3}
    assert (result["groups"]["ocr"]["C"], result["groups"]["ocr"]["T"]) == (2, 3)
    assert result["groups"]["error"]["cer"]["false_positive_chars"] == 1
    assert result["groups"]["error"]["cer"]["micro"] is None
    assert result["hot_latencies"]["all"]["N"] == 10 and result["stability"]["stable"]
    assert result["cold"]["startup"]["p50_ns"] == 10
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    result["resources"][0]["individual_hwm_bytes"].append(1)
    assert resources[0]["individual_hwm_bytes"] == [100, 50]
    # A later improved output can only mark instability; it cannot improve the first score.
    records[6]["output"]["parsed"]["fields"].append(field("header.tax", "1.00"))
    changed = summarize_candidate(cases, records)
    assert changed["groups"]["ocr"]["C"] == 2 and not changed["stability"]["stable"]


@pytest.mark.parametrize("change", ["box", "text", "reason", "bool"])
def test_full_output_stability_checks_more_than_scored_fields(change):
    records = [record(repeat=repeat) for repeat in range(5)]
    page = records[3]["output"]["pages"][0]
    if change == "box":
        page["words"] = [{"bbox": [1, 2, 3, 4], "text": "fictional"}]
    elif change == "text":
        records[3]["output"]["raw_text"] += " "
    elif change == "reason":
        page["reason_code"] = "unusable_text"
    else:
        page["page_index"] = False  # Python equality alone would hide 0 → False.
    result = validate_repeats([case()], records)
    assert result == {
        "stable": False,
        "reasons": [{"case_id": "fictional-a", "round": 3, "reason": "candidate_output_unstable"}],
    }


@pytest.mark.parametrize("fault", ["missing", "duplicate", "unknown", "sixth", "bool"])
def test_incomplete_or_duplicated_matrices_cannot_reduce_fixed_denominators(fault):
    records = [record(repeat=repeat) for repeat in range(5)]
    if fault == "missing":
        records.pop()
    elif fault == "duplicate":
        records.append(deepcopy(records[0]))
    elif fault == "unknown":
        records[0]["case_id"] = "not-frozen"
    elif fault == "sixth":
        records[0]["round"] = 5
    else:
        records[0]["round"] = False
    with pytest.raises(FormalReportError):
        summarize_candidate([case()], records)


def test_expected_ambiguity_must_be_review_not_missing_or_guessed():
    sample = case(expected={"header.total": "12.00"})
    sample["expected_review_paths"] = ["header.total"]
    assert summarize_document(sample, record())["expected_review"] == {"header.total": False}
    assert summarize_document(sample, record(fields=[]))["expected_review"] == {
        "header.total": False
    }
    actual = record(fields=[field("header.total", None, "review")])
    assert summarize_document(sample, actual)["expected_review"] == {"header.total": True}


def test_missing_model_evidence_has_no_invented_latency():
    actual = record("fictional-missing", status="failed")
    actual.update(submitted_ns=None, returned_ns=None)
    result = summarize_candidate([case("fictional-missing", "error")], [actual])
    assert result["groups"]["error"]["T"] == 1
    assert result["hot_latencies"]["all"] == {"N": 0, "p50_ns": None, "p95_ns": None}
    with pytest.raises(FormalReportError):
        summarize_latencies([actual])


def test_raw_intervals_preserve_nested_and_incomplete_failure_measurements():
    actual = record(status="failed")

    def event(phase, edge, at, outcome=None):
        return {
            "phase": phase,
            "page_index": 0,
            "edge": edge,
            "at_ns": at,
            "details": {"outcome": outcome} if outcome else None,
        }

    actual["output"]["timings_ns"]["events"] = [
        event("source", "start", 0),
        event("source", "end", 5, "passed"),
        event("prepare_total", "start", 5),
        event("pdf_render", "start", 7),
        event("pdf_render", "end", 15, "passed"),
        event("sdk_recognize", "start", 20),
        event("sdk_recognize", "end", 30, "failed"),
        event("prepare_total", "end", 35, "failed"),
        event("parse", "start", 40),  # killed here; no END must mean no measured duration
    ]
    result = summarize_latencies([actual])
    assert result["phases"]["prepare_total"]["p50_ns"] == 30
    assert result["phases"]["sdk_recognize"]["p50_ns"] == 10
    assert result["phases"]["sdk_recognize"]["failed_intervals"] == 1
    assert result["phases"]["source"]["N"] == 1  # legacy source was not counted twice
    assert result["phases"]["parse"] == {
        "N": 0,
        "p50_ns": None,
        "p95_ns": None,
        "failed_intervals": 0,
    }
    assert result["incomplete_phases"] == {"parse": 1}
    assert result["all"]["N"] == 1 and result["success"]["N"] == 0


@pytest.mark.parametrize(
    "events",
    [
        [{"phase": "parse", "page_index": None, "edge": "end", "at_ns": 1}],
        [
            {"phase": "parse", "page_index": None, "edge": "start", "at_ns": 2},
            {"phase": "parse", "page_index": None, "edge": "end", "at_ns": 1},
        ],
        [{"phase": "parse", "page_index": None, "edge": "start", "at_ns": 1}] * 2,
    ],
)
def test_invalid_raw_phase_pairs_are_not_silently_filled_or_reordered(events):
    actual = record()
    actual["output"]["timings_ns"]["events"] = events
    with pytest.raises(FormalReportError):
        summarize_latencies([actual])
