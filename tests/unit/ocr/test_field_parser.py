"""Independent fictional words and coordinates exercise extraction, never formal truth."""

from copy import deepcopy

import pytest


def word(text, x, y, width=100):
    from coinpup_api.ocr.parser_types import Word

    return Word(text, (x, y, x + width, y + 12))


def page(lines=(), *, extra=(), width=900, height=1400, reverse=False, text=""):
    from coinpup_api.ocr.parser_types import TextPage

    words = tuple(word(line, 15, 15 + index * 22, width - 30) for index, line in enumerate(lines))
    words += tuple(extra)
    return TextPage(width, height, tuple(reversed(words)) if reverse else words, text)


def parsed(*pages, **options):
    from coinpup_api.ocr.field_parser import parse_document

    return parse_document(pages, **options)


def fields(result):
    assert result["version"] == 1
    by_path = {field["path"]: field for field in result["fields"]}
    assert len(by_path) == len(result["fields"])
    return by_path


def certain(result, path, value):
    item = fields(result)[path]
    assert item["value"] == value and item["status"] == "certain" and item["reason"] is None
    assert item["evidence"] and all(
        set(evidence) == {"page", "bbox", "raw"} for evidence in item["evidence"]
    )
    return item


def review(result, path, reason, value=None):
    item = fields(result)[path]
    assert item["value"] == value and item["status"] == "review" and item["reason"] == reason
    assert result["status"] == "review"
    return item


@pytest.mark.parametrize(
    "labels",
    [
        ("Date", "Currency", "Subtotal", "Tax", "Total"),
        ("日期", "币种", "小计", "税额", "合计"),
        ("日期", "幣種", "小計", "稅額", "合計"),
        ("日期 Date", "币种 Currency", "小计 Subtotal", "税额 Tax", "合计 Total"),
    ],
)
def test_public_labels_extract_independent_invoice_without_english_fallback(labels):
    date, currency, subtotal, tax, total = labels
    result = parsed(
        page(
            [
                f"{date}: 2031-07-18",
                f"{currency}: EUR",
                f"{subtotal}: 0042.60",
                f"{tax}: 1.20",
                f"{total}: +43.80",
            ],
            reverse=True,
        )
    )
    for path, value in {
        "document_date": "2031-07-18",
        "currency": "EUR",
        "subtotal": "42.60",
        "tax": "1.20",
        "total": "43.80",
    }.items():
        certain(result, "header." + path, value)
    assert not result["rows"]


def test_subtotal_is_not_total_and_missing_labels_do_not_generate_fields_or_defaults():
    result = parsed(page(["Subtotal: 8.00", "Tax: 1.00", "Fictional item: 999.00"]))
    assert set(fields(result)) == {"header.subtotal", "header.tax"}
    assert "header.total" not in fields(result) and "header.currency" not in fields(result)


@pytest.mark.parametrize(
    "raw,value,requires_review",
    [
        ("+001,234.5600", "1234.56", False),
        ("-00045.678", "-45.678", True),
        ("+0.00", "0.00", False),
        ("+0012", "12.00", False),
        ("1.2", "1.20", False),
        (
            "99999999999999999999.123456789012345678",
            "99999999999999999999.123456789012345678",
            True,
        ),
    ],
)
def test_amounts_keep_sign_and_nonzero_precision_without_binary_or_decimal_rounding(
    raw, value, requires_review
):
    result = parsed(page(["Total: " + raw]))
    item = (
        review(result, "header.total", "excessive_precision", value)
        if requires_review
        else certain(result, "header.total", value)
    )
    assert raw in item["evidence"][0]["raw"]


@pytest.mark.parametrize(
    "raw",
    ["12,34.50", "1O.50", "1e3", "１２.３０", "100000000000000000000", "1.0000000000000000001"],
)
def test_bad_or_out_of_range_amounts_are_review_never_corrected_or_rounded(raw):
    review(parsed(page(["Total: " + raw])), "header.total", "invalid_amount")


@pytest.mark.parametrize("raw", ["01/02/2031", "2031-02-29", "2031-2-03"])
def test_only_valid_unambiguous_iso_dates_are_certain(raw):
    review(parsed(page(["Date: " + raw])), "header.document_date", "invalid_date")


@pytest.mark.parametrize("currency", ["CNY", "USD", "HKD", "GBP", "EUR"])
def test_explicit_supported_currency_codes_are_read_from_the_source(currency):
    certain(parsed(page(["Currency: " + currency])), "header.currency", currency)


@pytest.mark.parametrize("currency", ["$", "US$", "ZZZ"])
def test_unknown_currency_is_not_borrowed_from_adjacent_text(currency):
    result = parsed(page(["Currency: " + currency, "Fictional note USD", "Total: 6.00"]))
    review(result, "header.currency", "unknown_currency")
    certain(result, "header.total", "6.00")


def test_repeated_equivalent_headers_keep_all_evidence_and_conflicts_do_not_vote():
    result = parsed(page(["Total: +0010.00"]), page(["Total: 10.00"]))
    item = certain(result, "header.total", "10.00")
    assert [evidence["page"] for evidence in item["evidence"]] == [0, 1]
    conflict = parsed(page(["Total: 10.00", "Total: 10.00", "Total: 11.00"]))
    assert len(review(conflict, "header.total", "conflicting_field")["evidence"]) == 3


def test_split_label_tokens_with_small_baseline_offsets_group_without_mutating_input():
    words = [
        word("Opening", 20, 100, 50),
        word("balance", 75, 101, 45),
        word("EUR", 130, 100),
        word(":", 232, 101, 8),
        word("001,234.00", 250, 100),
        word("Total", 20, 150, 40),
        word(":", 65, 151, 8),
        word("-5.25", 90, 150),
    ]
    source = page(extra=words, reverse=True)
    before = deepcopy(source)
    result = parsed(source)
    certain(result, "header.opening_balance_eur", "1234.00")
    certain(result, "header.total", "-5.25")
    assert source == before


def table_words(rows, *, y=230):
    values = [
        word(label, x, y)
        for label, x in zip(
            ["Date", "Currency", "Amount", "Description"], [20, 220, 350, 520], strict=True
        )
    ]
    for index, row in enumerate(rows):
        for text, x in zip(row, [20, 220, 350, 520], strict=True):
            if text:
                values.append(word(text, x, y + (index + 1) * 24))
    return values


def test_identical_amount_rows_stay_distinct_and_bad_row_does_not_shift_later_ordinals():
    result = parsed(
        page(
            extra=table_words(
                [
                    ("2031-01-01", "GBP", "5.00", "Fictional A"),
                    ("2031-01-02", "GBP", "broken", "Fictional B"),
                    ("2031-01-03", "GBP", "5.00", "Fictional C"),
                ]
            )
        )
    )
    assert [(row["page"], row["table"], row["row"]) for row in result["rows"]] == [
        (0, 0, 0),
        (0, 0, 1),
        (0, 0, 2),
    ]
    certain(result, "rows.0.0.0.amount", "5.00")
    review(result, "rows.0.0.1.amount", "invalid_amount")
    certain(result, "rows.0.0.2.amount", "5.00")


def test_missing_row_cell_is_not_filled_from_a_neighbor_or_balance_equation():
    result = parsed(
        page(
            ["Opening balance GBP: 10.00", "Closing balance GBP: 15.00"],
            extra=table_words(
                [
                    ("2031-01-01", "", "", "Fictional missing"),
                    ("2031-01-02", "GBP", "5.00", "Fictional complete"),
                ]
            ),
        )
    )
    review(result, "rows.0.0.0.currency", "missing_field")
    review(result, "rows.0.0.0.amount", "missing_field")
    certain(result, "rows.0.0.1.amount", "5.00")


def test_invoice_arithmetic_mismatch_diagnoses_but_never_repairs_the_observed_total():
    result = parsed(page(["Subtotal: 4.00", "Tax: 1.00", "Total: 9.00"]))
    assert result["status"] == "review" and "arithmetic_mismatch" in result["diagnostics"]
    for role, value in (("subtotal", "4.00"), ("tax", "1.00"), ("total", "9.00")):
        certain(result, "header." + role, value)


def test_opposing_currency_balance_errors_cannot_cancel_each_other():
    result = parsed(
        page(
            [
                "Opening balance USD: 1.00",
                "Closing balance USD: 2.00",
                "Opening balance GBP: 5.00",
                "Closing balance GBP: 4.00",
            ],
            extra=table_words(
                [
                    ("2031-01-01", "USD", "0.00", "Fictional A"),
                    ("2031-01-02", "GBP", "0.00", "Fictional B"),
                ]
            ),
        )
    )
    assert "arithmetic_mismatch" in result["diagnostics"]
    certain(result, "header.closing_balance_usd", "2.00")
    certain(result, "header.closing_balance_gbp", "4.00")
    certain(result, "rows.0.0.0.amount", "0.00")
    certain(result, "rows.0.0.1.amount", "0.00")


def test_cross_column_word_marks_its_row_review_and_keeps_the_next_row_ordinal():
    words = table_words([])
    words += [
        word("2031-01-01", 20, 254),
        word("USD", 220, 254, 30),
        word("999.00", 300, 254, 140),
        word("Fictional ambiguous", 520, 254),
    ]
    words += [
        word("2031-01-02", 20, 278),
        word("USD", 220, 278),
        word("1.00", 350, 278),
        word("Fictional clear", 520, 278),
    ]
    result = parsed(page(extra=words))
    for role in ("date", "currency", "amount"):
        review(result, "rows.0.0.0." + role, "ambiguous_layout")
    certain(result, "rows.0.0.1.amount", "1.00")
    assert [row["row"] for row in result["rows"]] == [0, 1]


def test_pdf_page_boundaries_always_reset_table_and_row_ordinals():
    result = parsed(
        page(extra=table_words([("2031-04-01", "USD", "-8.00", "Fictional A")])),
        page(extra=table_words([("2031-04-02", "USD", "8.00", "Fictional B")])),
    )
    certain(result, "rows.0.0.0.amount", "-8.00")
    certain(result, "rows.1.0.0.amount", "8.00")
    assert [(row["page"], row["row"]) for row in result["rows"]] == [(0, 0), (1, 0)]


@pytest.mark.parametrize(
    "case,continuation",
    [
        ("valid", True),
        ("no_marker", False),
        ("counter_gap", False),
        ("header_conflict", False),
        ("column_shift", False),
    ],
)
def test_same_physical_page_continuation_uses_visible_markers_columns_and_no_header_conflict(
    case, continuation
):
    first = table_words([("2031-05-01", "USD", "1.00", "Fictional A")], y=140)
    second = table_words([("2031-05-02", "USD", "2.00", "Fictional B")], y=500)
    marker = (
        []
        if case == "no_marker"
        else [
            word("Page 1 of 3" if case == "counter_gap" else "Page 1 of 2", 20, 110),
            word("Page 3 of 3" if case == "counter_gap" else "Page 2 of 2", 20, 470),
        ]
    )
    if case == "header_conflict":
        marker.append(word("Currency: EUR", 20, 440))
    if case == "column_shift":
        from coinpup_api.ocr.parser_types import Word

        second = [
            Word(item.text, (item.bbox[0] + 80, item.bbox[1], item.bbox[2] + 80, item.bbox[3]))
            for item in second
        ]
    result = parsed(page(["Currency: USD"], extra=first + marker + second, reverse=True))
    certain(result, "rows.0.0.0.amount", "1.00")
    certain(result, "rows.0.0.1.amount" if continuation else "rows.0.1.0.amount", "2.00")


def test_empty_input_has_no_fabricated_financial_fields():
    result = parsed(page(["Fictional unrelated words only"]))
    assert result["reason"] == "no_fields" and result["fields"] == [] and result["rows"] == []
