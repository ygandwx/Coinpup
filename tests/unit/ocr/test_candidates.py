"""Fictional shared-parser output tests draft identity, evidence and review boundaries."""

from copy import deepcopy

import pytest
from coinpup_api.ledger.service import LedgerError
from coinpup_api.ocr.candidates import completion_from_result
from coinpup_api.ocr.contracts import prepare_completion
from coinpup_api.ocr.field_parser import parse_document
from coinpup_api.ocr.runtime import field_review

from tests.unit.ocr.test_field_parser import page


def result(*pages, ocr=()):
    value = {
        "version": 1,
        "status": "processed",
        "reason": None,
        "raw_text": "\n\n".join(p.text for p in pages),
        "parsed": parse_document(pages),
        "pages": [
            {
                "page_index": index,
                "layer": "absent" if index in ocr else "present",
                "route": "render" if index in ocr else "extract",
                "text": p.text,
                "words": [{"text": w.text, "bbox": list(w.bbox)} for w in p.words],
            }
            for index, p in enumerate(pages)
        ],
        "timings_ns": {"prepare": 1, "recognize": 0, "parse": 1},
    }
    value["field_review"] = field_review(value)
    return value


def statement(*amounts, ocr=()):
    lines = ["Date | Currency | Amount", *(f"2031-07-18 | USD | {a}" for a in amounts)]
    return result(page(lines, text="\n".join(lines)), ocr=ocr)


def test_repeated_amounts_are_separate_stable_rows_and_never_financial_commands():
    original = statement("10.00", "10.00", "30.00", ocr=(0,))
    before = deepcopy(original)
    completion = completion_from_result(original)
    assert len(completion.candidates) == 3
    keys = [candidate.source_key for candidate in completion.candidates]
    assert keys == [f"page:0/table:0/row:{row}" for row in range(3)]
    assert original == before
    assert completion.summary["recognition"] == before
    assert prepare_completion(completion) == prepare_completion(completion_from_result(before))
    for candidate in completion.candidates:
        assert candidate.recognized["kind"] == "statement_row"
        assert candidate.fields["confirmed"] == []
        assert all(f["requires_confirmation"] for f in candidate.fields["review"])
        assert all(f["suggested_value"] is None for f in candidate.fields["review"])
        assert candidate.evidence == {"version": 1, "pages": [0]}
        assert not {"account_id", "category_id", "direction", "amount"} & candidate.fields.keys()
    original["pages"][0]["text"] = "Fictional later edit"
    assert completion.summary["recognition"] == before


def test_invalid_row_is_retained_without_zero_and_header_totals_do_not_create_a_fourth_draft():
    lines = [
        "Total: 60.00",
        "Opening balance USD: 100.00",
        "Date | Currency | Amount",
        "2031-07-18 | USD | 10.00",
        "2031-07-18 | USD | not-a-number",
        "2031-07-18 | USD | 30.00",
    ]
    value = result(page(lines, text="\n".join(lines)))
    completion = completion_from_result(value)
    assert len(completion.candidates) == 3
    broken = completion.candidates[1]
    amount = next(f for f in broken.recognized["fields"] if f["path"].endswith("amount"))
    assert amount["value"] is None and amount["status"] == "review"
    assert amount["evidence"][0]["raw"] == " not-a-number"
    assert all(
        f["path"].startswith("rows.") for c in completion.candidates for f in c.recognized["fields"]
    )
    assert any(
        f["path"] == "header.total" for f in completion.summary["recognition"]["parsed"]["fields"]
    )


def test_invoice_is_one_document_with_exact_text_and_raw_amount():
    value = result(page(["Currency: USD", "Total: -001,234.00"], text="Fictional 原文"))
    completion = completion_from_result(value)
    (candidate,) = completion.candidates
    assert candidate.source_key == "document:0" and candidate.recognized["kind"] == "document"
    amount = next(f for f in candidate.fields["review"] if f["path"] == "header.total")
    assert amount["candidate_value"] == amount["suggested_value"] == "-1234.00"
    assert amount["requires_confirmation"] is False and candidate.fields["confirmed"] == []
    assert completion.summary["recognition"]["raw_text"] == "Fictional 原文"
    assert candidate.recognized["fields"][1]["evidence"][0]["raw"] == " -001,234.00"


def test_mixed_document_keeps_page_source_and_non_row_pages_for_manual_review():
    lines = ["Date | Currency | Amount", "2031-07-18 | USD | 20.00"]
    value = result(page(lines), page(lines), page(["Fictional unparsed note"]), ocr=(1,))
    candidates = completion_from_result(value).candidates
    assert len(candidates) == 3
    assert candidates[0].source_key == "page:0/table:0/row:0"
    assert candidates[1].source_key == "page:1/table:0/row:0"
    assert candidates[2].source_key == "page:2/manual"
    assert all(not f["requires_confirmation"] for f in candidates[0].fields["review"])
    assert all(f["requires_confirmation"] for f in candidates[1].fields["review"])
    assert candidates[2].fields == {"version": 1, "review": [], "confirmed": []}


@pytest.mark.parametrize("layer", ["absent", "present", "unknown"])
def test_manual_page_without_fields_stays_an_explicit_empty_document(layer):
    value = result(page())
    value.update(status="manual", reason="unsupported_input")
    value["pages"][0].update(layer=layer, route="manual")
    completion = completion_from_result(value)
    assert completion.summary["manual_pages"] == 1
    assert len(completion.candidates) == 1
    assert completion.candidates[0].fields["review"] == []
    assert completion.summary["recognition"]["reason"] == "unsupported_input"


@pytest.mark.parametrize(
    "damage",
    [
        "prefill",
        "duplicate_row",
        "duplicate_field",
        "missing_field",
        "page",
        "bool_page",
        "float_amount",
        "failed",
        "path",
        "missing_evidence_page",
    ],
)
def test_malformed_or_forged_output_is_rejected_without_echoing_originals(damage):
    value = statement("10.00", ocr=(0,))
    if damage == "prefill":
        value["field_review"][0].update(requires_confirmation=False, suggested_value="2031-07-18")
    elif damage == "duplicate_row":
        value["parsed"]["rows"] *= 2
    elif damage == "duplicate_field":
        value["parsed"]["fields"] *= 2
    elif damage == "missing_field":
        value["parsed"]["fields"].pop()
    elif damage == "page":
        value["pages"][0]["page_index"] = 10
    elif damage == "bool_page":
        value["pages"][0]["page_index"] = False
    elif damage == "float_amount":
        value["parsed"]["fields"][-1]["value"] = 10.0
    elif damage == "failed":
        value["status"] = "failed"
    elif damage == "path":
        value["parsed"]["fields"][-1]["path"] = "rows.0.0.9.amount"
    else:
        value["parsed"]["fields"][0]["evidence"][0]["page"] = 1
    value["raw_text"] = "Fictional private original"
    with pytest.raises(LedgerError) as caught:
        completion_from_result(value)
    assert caught.value.code == "ocr_invalid_payload"
    assert "Fictional" not in str(caught.value)


def test_combined_result_budget_rejects_without_truncation():
    value = result(page(["Total: 10.00"]))
    value["raw_text"] = "Fictional " * 110000
    value["pages"][0]["text"] = "Fictional " * 110000
    with pytest.raises(LedgerError):
        completion_from_result(value)
    assert len(value["raw_text"]) == 1100000


def test_candidate_budget_cannot_be_bypassed_by_smaller_document_envelope():
    value = result(page(["Total: 10.00"]))
    value["parsed"]["fields"][0]["evidence"][0]["raw"] = "Fictional " * 30000
    with pytest.raises(LedgerError):
        completion_from_result(value)


def test_two_hundred_rows_plus_unparsed_page_cannot_silently_drop_last_page():
    lines = ["Date | Currency | Amount", *["2031-07-18 | USD | 10.00"] * 200]
    value = result(page(lines, height=6000), page())
    assert len(value["parsed"]["rows"]) == 200
    with pytest.raises(LedgerError):
        completion_from_result(value)
    assert len(value["pages"]) == 2
