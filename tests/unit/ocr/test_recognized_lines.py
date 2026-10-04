"""Handwritten fictional recognized lines; no corpus, engine output or expected-template input."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest
from coinpup_api.ocr.field_parser import parse_document
from coinpup_api.ocr.parser_types import ParserLimits, TextPage, Word

HEADER = "Date | Currency | Amount | Description"


def line(text, y, x0=20, x1=780):
    return Word(text, (x0, y, x1, y + 12))


def page(*words):
    return TextPage(900, 1400, tuple(words))


def table(*rows, header=HEADER, y=80):
    return [line(header, y), *(line(row, y + (i + 1) * 24) for i, row in enumerate(rows))]


def fields(result):
    assert result["version"] == 1
    values = {item["path"]: item for item in result["fields"]}
    assert len(values) == len(result["fields"])
    return values


def certain(result, path, value):
    item = fields(result)[path]
    assert (item["value"], item["status"], item["reason"]) == (value, "certain", None)
    return item


def review(result, path, reason, value=None):
    item = fields(result)[path]
    assert (item["value"], item["status"], item["reason"]) == (value, "review", reason)
    assert result["status"] == "review"
    return item


@pytest.mark.parametrize(
    "header",
    [
        HEADER,
        "日期 | 币种 | 金额 | 说明",
        "日期 | 幣種 | 金額 | 說明",
        "日期 Date | 币种 Currency | 金额 Amount | 说明 Description",
    ],
)
def test_actual_whole_lines_keep_exact_money_raw_signs_and_coarse_evidence(header):
    words = table(
        "2033-06-01 | GBP | -1,234.00 | Fictional debit",
        "2033-06-02 | GBP | +001,234.00 | Fictional credit",
        header=header,
    )
    source = page(*reversed(words))
    original = deepcopy(source)
    result = parse_document((source,))
    assert result["status"] == "parsed" and source == original
    assert [row["row"] for row in result["rows"]] == [0, 1]
    for index, (amount, raw) in enumerate([("-1234.00", "-1,234.00"), ("1234.00", "+001,234.00")]):
        prefix = f"rows.0.0.{index}."
        certain(result, prefix + "date", f"2033-06-0{index + 1}")
        certain(result, prefix + "currency", "GBP")
        certain(result, prefix + "amount", amount)
        for role in ("date", "currency", "amount"):
            evidence = fields(result)[prefix + role]["evidence"]
            assert len(evidence) == 1 and evidence[0]["bbox"] == list(words[index + 1].bbox)
            assert evidence[0]["page"] == 0
        assert fields(result)[prefix + "amount"]["evidence"][0]["raw"].strip() == raw


@pytest.mark.parametrize(
    "header,row,description",
    [
        ("Currency | Amount | Date", "EUR | -2.50 | 2033-07-04", ""),
        (
            "Description | Amount | Currency | Date",
            "Fictional expense | -2.50 | EUR | 2033-07-04",
            "Fictional expense",
        ),
    ],
)
def test_literal_role_order_maps_cells_without_guessing_columns(header, row, description):
    result = parse_document((page(*table(row, header=header)),))
    certain(result, "rows.0.0.0.date", "2033-07-04")
    certain(result, "rows.0.0.0.currency", "EUR")
    certain(result, "rows.0.0.0.amount", "-2.50")
    assert result["rows"][0]["description"] == description


@pytest.mark.parametrize(
    "date,currency,amount,role,reason,value",
    [
        ("2033-07-04", "USD", "- 1.00", "amount", "invalid_amount", None),
        ("2033-07-04", "USD", "1O.00", "amount", "invalid_amount", None),
        ("2033-07-04", "USD", "1.234", "amount", "excessive_precision", "1.234"),
        ("01/02/2033", "USD", "1.00", "date", "invalid_date", None),
        ("2033-02-29", "USD", "1.00", "date", "invalid_date", None),
        ("2033-07-04", "$", "1.00", "currency", "unknown_currency", None),
    ],
)
def test_coarse_cells_use_the_original_strict_value_rules(
    date, currency, amount, role, reason, value
):
    result = parse_document(
        (page(line("Currency: USD", 20), *table(f"{date} | {currency} | {amount} | Fictional")),)
    )
    review(result, "rows.0.0.0." + role, reason, value)
    if role != "amount":
        certain(result, "rows.0.0.0.amount", "1.00")


@pytest.mark.parametrize(
    "bad_row",
    [
        "2033-08-01 | GBP | 1.00",
        "2033-08-01 | GBP | 1.00 | Fictional | extra",
        "| 2033-08-01 | GBP | 1.00 | Fictional",
        "2033-08-01 | GBP | 1.00 | Fictional |",
        "Fictional row with no separators",
    ],
)
def test_bad_cell_count_never_shifts_values_or_drops_the_printed_row(bad_row):
    result = parse_document((page(*table(bad_row, "2033-08-02 | GBP | 5.00 | Fictional next")),))
    assert [row["row"] for row in result["rows"]] == [0, 1]
    for role in ("date", "currency", "amount"):
        review(result, "rows.0.0.0." + role, "ambiguous_layout")
    certain(result, "rows.0.0.1.amount", "5.00")


@pytest.mark.parametrize("role,index", [("date", 0), ("currency", 1), ("amount", 2)])
def test_empty_cell_is_missing_even_when_a_neighbor_or_header_has_a_valid_value(role, index):
    cells = ["2033-08-01", "GBP", "5.00", "Fictional empty cell"]
    cells[index] = ""
    result = parse_document((page(line("Currency: GBP", 20), *table(" | ".join(cells))),))
    review(result, "rows.0.0.0." + role, "missing_field")
    assert len(result["rows"]) == 1


@pytest.mark.parametrize(
    "bad_header",
    [
        "Date | Currency | Amount | Amount",
        "Date | Currency | Balance | Description",
        "Currency | Amount | Description",
        "| Date | Currency | Amount | Description |",
        "Fictional note | GBP | 5.00",
    ],
)
def test_duplicate_unknown_or_incomplete_labels_do_not_activate_a_table(bad_header):
    result = parse_document(
        (
            page(
                line("Total: 6.00", 20),
                *table("2033-08-01 | GBP | 5.00 | Fictional", header=bad_header),
            ),
        )
    )
    assert result["rows"] == [] and set(fields(result)) == {"header.total"}
    certain(result, "header.total", "6.00")


@pytest.mark.parametrize(
    "bad_header", ["Date | Currency | Amount | Amount", "Date | Currency | Balance | Description"]
)
def test_malformed_visible_header_stops_reuse_of_the_previous_table_roles(bad_header):
    words = table("2033-08-01 | GBP | 5.00 | Fictional first")
    words.extend([line(bad_header, 128), line("2033-08-02 | GBP | 999.00 | Fictional later", 152)])
    result = parse_document((page(*words),))
    assert result["status"] == "review" and "ambiguous_layout" in result["diagnostics"]
    assert len(result["rows"]) == 1
    assert set(fields(result)) == {"rows.0.0.0." + role for role in ("date", "currency", "amount")}
    certain(result, "rows.0.0.0.amount", "5.00")


def test_identical_printed_rows_remain_distinct_candidates_with_both_original_boxes():
    raw = "2033-08-01 | GBP | 5.00 | Fictional repeated transaction"
    words = table(raw, raw)
    result = parse_document((page(*words),))
    assert [(row["table"], row["row"]) for row in result["rows"]] == [(0, 0), (0, 1)]
    for index in (0, 1):
        item = certain(result, f"rows.0.0.{index}.amount", "5.00")
        assert item["evidence"][0]["bbox"] == list(words[index + 1].bbox)


def test_two_word_fragments_cannot_be_joined_into_an_invented_single_line_table_row():
    words = table()
    words.extend(
        [
            line("2033-08-01 | GBP", 104, 20, 300),
            line("| 5.00 | Fictional", 104, 320, 780),
            line("2033-08-02 | GBP | 5.00 | Fictional next", 128),
        ]
    )
    result = parse_document((page(*words),))
    for role in ("date", "currency", "amount"):
        review(result, "rows.0.0.0." + role, "ambiguous_layout")
    certain(result, "rows.0.0.1.amount", "5.00")


@pytest.mark.parametrize(
    "case,continuation",
    [
        ("same", True),
        ("equal_header", True),
        ("no_counter", False),
        ("counter_gap", False),
        ("header_conflict", False),
        ("role_order", False),
        ("horizontal_shift", False),
        ("extent_change", False),
        ("mode_switch", False),
    ],
)
def test_photo_continuation_needs_literal_roles_real_extents_markers_and_no_conflict(
    case, continuation
):
    words = [line("Currency: GBP", 20)]
    if case != "no_counter":
        words.append(line("Page 1 of 3" if case == "counter_gap" else "Page 1 of 2", 45))
    words += table("2033-09-01 | GBP | 1.00 | Fictional first")
    if case in ("equal_header", "header_conflict"):
        words.append(line("Currency: EUR" if case == "header_conflict" else "Currency: GBP", 275))
    if case != "no_counter":
        words.append(line("Page 3 of 3" if case == "counter_gap" else "Page 2 of 2", 310))
    second = table("2033-09-02 | GBP | 2.00 | Fictional second", y=345)
    if case == "role_order":
        second = table(
            "GBP | 2033-09-02 | 2.00 | Fictional second",
            header="Currency | Date | Amount | Description",
            y=345,
        )
    elif case in ("horizontal_shift", "extent_change"):
        second = [
            line(word.text, word.bbox[1], 70 if case == "horizontal_shift" else 20, 830)
            for word in second
        ]
    elif case == "mode_switch":
        second = [
            line("Date", 345, 20, 120),
            line("Currency", 345, 220, 320),
            line("Amount", 345, 350, 450),
            line("Description", 345, 520, 620),
            line("2033-09-02", 369, 20, 120),
            line("GBP", 369, 220, 250),
            line("2.00", 369, 350, 450),
            line("Fictional second", 369, 520, 700),
        ]
    result = parse_document((page(*reversed(words + second)),))
    certain(result, "rows.0.0.0.amount", "1.00")
    target = "rows.0.0.1.amount" if continuation else "rows.0.1.0.amount"
    certain(result, target, "2.00")
    assert [(row["table"], row["row"]) for row in result["rows"]] == (
        [(0, 0), (0, 1)] if continuation else [(0, 0), (1, 0)]
    )
    if case == "header_conflict":
        review(result, "header.currency", "conflicting_field")


def test_physical_pdf_pages_reset_ordinals_even_with_continuous_printed_page_markers():
    pages = (
        page(line("Page 1 of 2", 45), *table("2033-09-01 | USD | 7.00 | Fictional first")),
        page(line("Page 2 of 2", 45), *table("2033-09-02 | USD | 7.00 | Fictional second")),
    )
    result = parse_document(pages)
    certain(result, "rows.0.0.0.amount", "7.00")
    certain(result, "rows.1.0.0.amount", "7.00")
    assert [(row["page"], row["table"], row["row"]) for row in result["rows"]] == [
        (0, 0, 0),
        (1, 0, 0),
    ]


def test_a_literal_pipe_in_a_finer_cross_column_word_does_not_rescue_geometry_ambiguity():
    words = [
        line("Date", 80, 20, 120),
        line("Currency", 80, 220, 320),
        line("Amount", 80, 350, 450),
        line("Description", 80, 520, 620),
        line("2033-10-01", 104, 20, 120),
        line("GBP", 104, 220, 250),
        line("999.00 |", 104, 300, 480),
        line("Fictional ambiguous", 104, 520, 700),
        line("2033-10-02", 128, 20, 120),
        line("GBP", 128, 220, 250),
        line("1.00", 128, 350, 450),
        line("Fictional clear", 128, 520, 700),
    ]
    result = parse_document((page(*words),))
    for role in ("date", "currency", "amount"):
        review(result, "rows.0.0.0." + role, "ambiguous_layout")
    certain(result, "rows.0.0.1.amount", "1.00")


@pytest.mark.parametrize(
    "limit,maximum",
    [("max_rows", 1), ("max_fields", 2), ("evidence_bytes", 1), ("output_bytes", 128)],
)
def test_whole_line_mode_preserves_existing_whole_result_budget_rejection(limit, maximum):
    source = page(
        *table("2033-10-01 | USD | 7.00 | Fictional A", "2033-10-02 | USD | 7.00 | Fictional B")
    )
    result = parse_document((source,), limits=replace(ParserLimits(), **{limit: maximum}))
    assert result == {
        "version": 1,
        "status": "review",
        "reason": "parser_limit",
        "fields": [],
        "rows": [],
        "diagnostics": ["parser_limit"],
    }
    if limit == "output_bytes":
        assert len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf8")) <= 128
