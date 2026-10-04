"""Pure reporting over frozen cases and actual outputs; never repair recognition."""

import json
import re
from collections import Counter, defaultdict
from copy import deepcopy
from fractions import Fraction

from .scoring import CharacterScore, aggregate_cer, character_errors, score_fields

_GROUPS = ("text", "ocr", "degraded", "error")
_BUDGETS = {"probe_limit", "prepare_limit", "parser_limit", "engine_limit", "resource_limit"}
_ROW = re.compile(r"rows\.(\d+)\.(\d+)\.(\d+)\.([a-z_]+)\Z")
_ROLES = ("date", "currency", "amount")


class FormalReportError(ValueError):
    def __init__(self):
        super().__init__("formal_report_invalid")


def _require(condition):
    if not condition:
        raise FormalReportError


def _integer(value):
    _require(type(value) is int and value >= 0)
    return value


def _ratio(value):
    return (
        None if value is None else {"numerator": value.numerator, "denominator": value.denominator}
    )


def _budget(value):
    if isinstance(value, dict):
        return any(
            (key in ("reason", "reason_code") and isinstance(item, str) and item in _BUDGETS)
            or (
                key == "diagnostics"
                and isinstance(item, list)
                and any(isinstance(code, str) and code in _BUDGETS for code in item)
            )
            or (isinstance(item, (dict, list)) and _budget(item))
            for key, item in value.items()
        )
    return isinstance(value, list) and any(_budget(item) for item in value)


def _failed(record):
    return (
        record.get("status") in ("failed", "timeout")
        or record["output"].get("status") in ("failed", "timeout")
        or _budget(record["output"])
    )


def project_result(record):
    """Keep useful manual-page output; genuine failures score as empty, never disappear."""
    output = record.get("output")
    _require(
        type(output) is dict
        and output.get("status") in ("processed", "manual", "failed", "timeout")
    )
    if _failed(record):
        return [], ""
    parsed = output.get("parsed")
    _require(type(parsed) is dict and type(parsed.get("fields")) is list)
    _require(type(output.get("raw_text")) is str)
    fields = []
    for field in parsed["fields"]:
        _require(type(field) is dict and {"path", "value", "status"} <= field.keys())
        fields.append({key: field[key] for key in ("path", "value", "status")})
    # Reuse the frozen validation, including path and value types, without normalization.
    score_fields({}, fields)
    return fields, output["raw_text"]


def nearest_rank(samples_ns, percentile):
    _require(type(percentile) is Fraction and 0 < percentile <= 1)
    ordered = sorted(_integer(sample) for sample in samples_ns)
    if not ordered:
        return None
    rank = (
        percentile.numerator * len(ordered) + percentile.denominator - 1
    ) // percentile.denominator
    return ordered[rank - 1]


def _distribution(samples):
    return {
        "N": len(samples),
        "p50_ns": nearest_rank(samples, Fraction(1, 2)),
        "p95_ns": nearest_rank(samples, Fraction(95, 100)),
    }


def _duration(start, end):
    start, end = _integer(start), _integer(end)
    _require(end >= start)
    return end - start


def _phase_intervals(events):
    active, durations, failed = {}, defaultdict(list), Counter()
    for event in events:
        _require(type(event) is dict)
        phase, page, edge = event.get("phase"), event.get("page_index"), event.get("edge")
        _require(type(phase) is str and (page is None or type(page) is int and page >= 0))
        key, at = (phase, page), _integer(event.get("at_ns"))
        if edge == "start":
            _require(key not in active)
            active[key] = at
        else:
            _require(edge == "end" and key in active)
            durations[phase].append(_duration(active.pop(key), at))
            failed[phase] += (event.get("details") or {}).get("outcome") == "failed"
    return durations, failed, Counter(phase for phase, _ in active)


def summarize_latencies(records):
    """All terminal waits are primary; partial/missing phase observations are not zeros."""
    all_delays, success, phases = [], [], defaultdict(list)
    phase_failures, incomplete = Counter(), Counter()
    for record in records:
        delay = _duration(record["submitted_ns"], record["returned_ns"])
        all_delays.append(delay)
        if not _failed(record) and record["output"]["status"] == "processed":
            success.append(delay)
        timings = record["output"].get("timings_ns", {})
        if "events" in timings:
            observed, failed, pending = _phase_intervals(timings["events"])
            for name, values in observed.items():
                phases[name].extend(values)
            phase_failures.update(failed)
            incomplete.update(pending)
            # Raw paired events are authoritative; legacy durations are not additional work.
            continue
        # failed_result's legacy zero durations are placeholders, not measured samples.
        if not _failed(record):
            for source, label in (
                ("prepare", "prepare_inclusive_residual"),
                ("recognize", "recognize"),
                ("parse", "parse"),
            ):
                if source in timings:
                    phases[label].append(_integer(timings[source]))
        source = timings.get("source", {})
        if "start" in source and "end" in source:
            phases["source"].append(_duration(source["start"], source["end"]))
    return {
        "all": _distribution(all_delays),
        "success": _distribution(success),
        "success_scope": "Processed without a budget failure; this is not a quality pass.",
        "phases": {
            name: {**_distribution(phases[name]), "failed_intervals": phase_failures[name]}
            for name in sorted(phases.keys() | incomplete.keys())
        },
        "incomplete_phases": dict(sorted(incomplete.items())),
        "phase_scope": (
            "Complete same-phase/page intervals only. prepare_total contains SDK work; "
            "nested stages must not be added together. IPC is only in host end-to-end time."
        ),
        "prepare_scope": (
            "Inclusive preparation residual: probe/extract/render/RGB/hash/observation/IPC; "
            "not pure rendering."
        ),
    }


def _rows(paths):
    rows = set()
    for path in paths:
        match = _ROW.fullmatch(path)
        if match:
            rows.add(tuple(map(int, match.groups()[:3])))
    return rows


def _certain(fields):
    counts = Counter(field["path"] for field in fields)
    return {
        field["path"]: field["value"]
        for field in fields
        if counts[field["path"]] == 1
        and field["status"] == "certain"
        and field["value"] is not None
    }


def _tables(expected, fields):
    known, observed, certain = (
        _rows(expected),
        _rows(field["path"] for field in fields),
        _certain(fields),
    )
    result = []
    for page, table in sorted({row[:2] for row in known | observed}):
        wanted = {row for row in known if row[:2] == (page, table)}
        seen = {row for row in observed if row[:2] == (page, table)}
        complete = correct = 0
        for row in wanted:
            prefix = "rows." + ".".join(map(str, row)) + "."
            paths = [prefix + role for role in _ROLES]
            _require(all(path in expected for path in paths))
            complete += all(path in certain for path in paths)
            correct += all(path in certain and certain[path] == expected[path] for path in paths)
        result.append(
            {
                "page": page,
                "table": table,
                "expected_rows": len(wanted),
                "complete_rows": complete,
                "correct_rows": correct,
                "missing_rows": len(wanted - seen),
                "extra_rows": len(seen - wanted),
                "complete_ratio": _ratio(Fraction(complete, len(wanted))) if wanted else None,
            }
        )
    return result


def _decimal(value):
    if (
        type(value) is not str
        or len(value) > 64
        or not re.fullmatch(r"-?\d+(?:\.\d+)?", value, re.ASCII)
    ):
        return None
    whole, _, fraction = value.partition(".")
    return int(whole + fraction), len(fraction)


def _equation(values, expected):
    parts = [_decimal(value) for value in [*values, expected]]
    if any(part is None for part in parts):
        return "not_checkable"
    scale = max(part[1] for part in parts)
    units = [value * 10 ** (scale - places) for value, places in parts]
    return "consistent" if sum(units[:-1]) == units[-1] else "mismatch"


def diagnose_predictions(fields):
    """Literal predicted arithmetic only; consistency never proves complete source coverage."""
    certain = _certain(fields)
    subtotal, tax, total = (certain.get("header." + name) for name in ("subtotal", "tax", "total"))
    balances = []
    rows = sorted(_rows(field["path"] for field in fields))
    prefixes = ["rows." + ".".join(map(str, row)) + "." for row in rows]
    for code in ("cny", "usd", "eur", "gbp", "hkd"):
        opening, closing = (
            "header." + name + "_" + code for name in ("opening_balance", "closing_balance")
        )
        if not any(field["path"] in (opening, closing) for field in fields):
            continue
        usable = all(
            prefix + role in certain for prefix in prefixes for role in ("currency", "amount")
        )
        amounts = (
            [
                certain[prefix + "amount"]
                for prefix in prefixes
                if certain.get(prefix + "currency") == code.upper()
            ]
            if usable
            else []
        )
        balances.append(
            {
                "currency": code.upper(),
                "status": _equation([certain.get(opening), *amounts], certain.get(closing))
                if usable
                else "not_checkable",
                "observed_rows": sum(
                    certain.get(prefix + "currency") == code.upper() for prefix in prefixes
                ),
            }
        )
    return {
        "scope": "Literal unique certain predictions only; missing source rows cannot be inferred.",
        "subtotal_plus_tax": _equation([subtotal, tax], total),
        "balances": balances,
    }


def summarize_document(case, record):
    fields, text = project_result(record)
    score = score_fields(case["expected_fields"], fields)
    cer = character_errors(case["reference_text"], text)
    counts = Counter(field["path"] for field in fields)
    expected = case["expected_fields"]
    return {
        "case_id": case["id"],
        "group": case["group"],
        "status": record["output"]["status"],
        "reason": record["output"].get("reason"),
        "scoring_projection": "empty_failure" if _failed(record) else "returned_prediction",
        "C": score.correct,
        "T": score.expected,
        "E": score.extra,
        "accuracy": _ratio(score.accuracy),
        "review": sum(field["status"] == "review" for field in fields),
        "missing": sorted(set(expected) - counts.keys()),
        "duplicates": {path: count for path, count in sorted(counts.items()) if count > 1},
        "extra_paths": {
            path: count for path, count in sorted(counts.items()) if path not in expected
        },
        "cer": {
            "edits": cer.edits,
            "reference_chars": cer.reference_chars,
            "ratio": _ratio(cer.ratio),
        },
        "tables": _tables(expected, fields),
        "arithmetic": diagnose_predictions(fields),
        "expected_review": {
            path: counts[path] == 1
            and any(field["path"] == path and field["status"] == "review" for field in fields)
            for path in case.get("expected_review_paths", [])
        },
    }


def _matrix(cases, records):
    identifiers = [case["id"] for case in cases]
    _require(len(set(identifiers)) == len(identifiers))
    _require(all(case["group"] in _GROUPS for case in cases))
    required = {
        (case["id"], repeat)
        for case in cases
        for repeat in (range(5) if case["group"] == "ocr" else (0,))
    }
    slots = {}
    for record in records:
        _require(type(record.get("round")) is int)
        key = record["case_id"], record["round"]
        _require(key in required and key not in slots)
        slots[key] = record
    _require(slots.keys() == required)
    return slots


def validate_repeats(cases, records):
    slots, different = _matrix(cases, records), []

    def semantic(output):
        return json.dumps(
            {key: value for key, value in output.items() if key != "timings_ns"},
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )

    for case in cases:
        if case["group"] != "ocr":
            continue
        first = semantic(slots[(case["id"], 0)]["output"])
        for repeat in range(1, 5):
            current = semantic(slots[(case["id"], repeat)]["output"])
            if current != first:
                different.append(
                    {"case_id": case["id"], "round": repeat, "reason": "candidate_output_unstable"}
                )
    return {"stable": not different, "reasons": different}


def summarize_candidate(cases, records, *, cold_records=(), resource_reports=()):
    slots = _matrix(cases, records)
    groups = {}
    for group in _GROUPS:
        documents = [
            summarize_document(case, slots[(case["id"], 0)])
            for case in cases
            if case["group"] == group
        ]
        correct, expected, extra = (
            sum(document[key] for document in documents) for key in ("C", "T", "E")
        )
        cer = aggregate_cer(
            CharacterScore(document["cer"]["edits"], document["cer"]["reference_chars"])
            for document in documents
        )
        groups[group] = {
            "C": correct,
            "T": expected,
            "E": extra,
            "accuracy": _ratio(Fraction(correct, expected + extra)) if expected + extra else None,
            "cer": {
                key: _ratio(value) if isinstance(value, Fraction) else value
                for key, value in cer.items()
            },
            "documents": documents,
        }
    ocr_ids = {case["id"] for case in cases if case["group"] == "ocr"}
    return {
        "version": 1,
        "quality_round": 0,
        "groups": groups,
        "stability": validate_repeats(cases, records),
        "hot_latencies": summarize_latencies(
            [record for record in records if record["case_id"] in ocr_ids]
        ),
        "cold": {
            "startup": _distribution(
                [_duration(record["started_ns"], record["ready_ns"]) for record in cold_records]
            ),
            "first_request": summarize_latencies([record["record"] for record in cold_records]),
        },
        "resources": deepcopy(list(resource_reports)),
    }
