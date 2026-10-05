"""Independent fictional boxes verify row presentation without repairing any field."""

import pytest
from coinpup_api.ocr.engine_adapters import _page
from coinpup_api.ocr.field_parser import parse_document
from coinpup_api.ocr.parser_types import TextPage


def test_jittered_cells_read_left_to_right_without_changing_source_boxes_or_parser():
    source = [
        ("Date", (10, 101, 40, 121)),
        ("Currency", (140, 100, 190, 121)),
        ("Amount", (290, 102, 340, 122)),
        ("2039-08-01", (10, 141, 85, 162)),
        ("GBP", (140, 144, 170, 164)),
        ("-01,200.00", (290, 140, 360, 161)),
    ]
    actual = _page(reversed(source), 600, 800)
    expected_words = tuple(sorted(actual.words, key=lambda word: (word.bbox[1], word.bbox[0])))
    assert [(word.text, word.bbox) for word in actual.words] == sorted(
        source, key=lambda item: (item[1][1], item[1][0])
    )
    assert actual.text == "Date Currency Amount\n2039-08-01 GBP -01,200.00"
    original = TextPage(600, 800, expected_words, "\n".join(word.text for word in expected_words))
    assert parse_document((actual,)) == parse_document((original,))
    assert (
        next(f for f in parse_document((actual,))["fields"] if f["path"].endswith(".amount"))[
            "value"
        ]
        == "-1200.00"
    )


@pytest.mark.parametrize(
    "source",
    [
        [("fictional first", (0, 0, 50, 20)), ("overlap", (40, 1, 90, 20))],
        [("fictional first", (0, 0, 50, 20)), ("next", (60, 15, 90, 35))],
    ],
)
def test_ambiguous_geometry_keeps_original_order_and_all_existing_rejections(source):
    actual = _page(source, 200, 200)
    assert actual.text == "\n".join(item[0] for item in source)
    original = TextPage(200, 200, actual.words, actual.text)
    assert parse_document((actual,)) == parse_document((original,))
    assert parse_document((actual,))["reason"] == "ambiguous_layout"


def test_no_delimiters_or_characters_are_invented_and_source_whitespace_survives():
    source = [
        (" 2039-08-01 ", (0, 0, 80, 20)),
        ("1", (90, 0, 95, 20)),
        (" GBP ", (100, 0, 150, 20)),
        ("-5.O0", (160, 0, 200, 20)),
    ]
    actual = _page(source, 220, 100)
    assert actual.text == " 2039-08-01  1  GBP  -5.O0"
    assert [(word.text, word.bbox) for word in actual.words] == source
    assert "|" not in actual.text and "5.00" not in actual.text
    assert (
        len(actual.text.encode("utf8")) == sum(len(text.encode("utf8")) for text, _ in source) + 3
    )


@pytest.mark.parametrize("currency,date", [("$", "01/08/2039"), ("USO", "2039-02-30")])
def test_row_presentation_cannot_rescue_unknown_currency_or_ambiguous_dates(currency, date):
    source = [
        ("Date", (0, 0, 50, 20)),
        ("Currency", (140, 0, 200, 20)),
        ("Amount", (300, 0, 360, 20)),
        (date, (0, 40, 100, 60)),
        (currency, (140, 40, 200, 60)),
        ("5.00", (300, 40, 350, 60)),
    ]
    actual = _page(source, 600, 100)
    original = TextPage(600, 100, actual.words, "\n".join(word.text for word in actual.words))
    assert parse_document((actual,)) == parse_document((original,))
    fields = {field["path"]: field for field in parse_document((actual,))["fields"]}
    assert fields["rows.0.0.0.currency"]["status"] == "review"
    assert fields["rows.0.0.0.date"]["status"] == "review"


def test_empty_and_single_real_source_line_keep_their_exact_text():
    assert _page([], 100, 100).text == ""
    source = "Date | Currency | Amount"
    assert _page([(source, (0, 0, 99, 20))], 100, 100).text == source
