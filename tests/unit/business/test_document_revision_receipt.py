"""Exact two-sided document facts, sparse originals and permanent route-bound raw intent."""

import json
from copy import deepcopy
from dataclasses import replace
from decimal import localcontext
from uuid import uuid4

import pytest
from coinpup_api.business.confirmation_hash import document_revision_hash_v2
from coinpup_api.business.document_revision_receipt import (
    DocumentRevisionResponse,
    verify_document_receipt_sources,
)
from coinpup_api.business.document_revision_schemas import (
    ConfirmedDocumentCancellation,
    ConfirmedDocumentCorrection,
)
from coinpup_api.business.draft_schemas import BusinessDraftLineInput, DraftHeaderInput
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.errors import MoneyError
from pydantic import ValidationError

from tests.unit.business.test_document_accrual_revision_plan import current, proposed


def sources_and_receipt(values=(1, 0, 3), entirely_new=False):
    doc, ops = current(values)
    heads = {o.source_line_id: o for o in ops}
    new_ids = [uuid4() for _ in values] if entirely_new else [row.id for row in reversed(doc.lines)]
    body = proposed(doc, [(i, "2.00") for i in new_ids])
    old = doc.model_dump(
        include=set(DraftHeaderInput.model_fields) | {"net_amount", "tax_amount", "total_amount"}
    )
    old["lines"] = []
    for row in doc.lines:
        head = heads.get(row.id)
        old["lines"].append(
            row.model_dump(
                include=set(BusinessDraftLineInput.model_fields)
                | {"line_no", "net_amount", "tax_amount", "total_amount"}
            )
            | {
                "posting": dict(
                    operation_id=head.operation_id,
                    operation_version=head.version,
                    journal_id=head.journal_id,
                )
                if head
                else None
            }
        )
    valued = body.price(doc.document_kind, ASSETS[body.asset_id])
    new = body.model_dump(include=set(DraftHeaderInput.model_fields))
    new.update(
        **{k + "_amount": getattr(valued, k).to_string() for k in ("net", "tax", "total")}, lines=[]
    )
    for pos, (row, price) in enumerate(zip(body.lines, valued.lines, strict=True), 1):
        head = heads.get(row.id)
        new["lines"].append(
            row.model_dump()
            | {
                "line_no": pos,
                **{k + "_amount": getattr(price, k).to_string() for k in ("net", "tax", "total")},
                "posting": dict(
                    operation_id=head.operation_id if head else uuid4(),
                    operation_version=head.version + 1 if head else 1,
                    journal_id=uuid4(),
                ),
            }
        )
    return (
        doc,
        ops,
        body,
        dict(
            kind="business_document_revision",
            action="correct",
            previous_version=doc.version,
            version=doc.version + 1,
            ledger_id=doc.ledger_id,
            document_id=doc.id,
            confirmation_id=doc.confirmation_id,
            revision_id=uuid4(),
            reason="Fictional corrected document",
            previous=old,
            replacement=new,
            reversals=[dict(source_line_id=o.source_line_id, journal_id=uuid4()) for o in ops],
        ),
    )


def example(values=(1, 0, 3), entirely_new=False):
    return sources_and_receipt(values, entirely_new)[-1]


@pytest.mark.parametrize("count,disjoint", [(3, False), (200, True)])
def test_complete_old_and_new_groups_preserve_order_amounts_and_exhausted_assets(count, disjoint):
    saved = example([1 if i % 3 else 0 for i in range(count)], disjoint)
    with localcontext() as ctx:
        ctx.prec = 2
        result = DocumentRevisionResponse.model_validate(saved)
        result.previous.verify_prices(replace(ASSETS["USD"], enabled=False))
        result.replacement.verify_prices(ASSETS["EUR"])
    encoded = result.model_dump_json()
    assert DocumentRevisionResponse.model_validate_json(encoded) == result
    cancelled = DocumentRevisionResponse.model_validate(
        saved | {"action": "cancel", "replacement": None}
    )
    assert len(cancelled.previous.lines) == count
    assert [v.source_line_id for v in cancelled.reversals] == [
        v.id for v in cancelled.previous.lines if v.posting
    ]
    assert len(result.previous.lines) + len(result.replacement.lines) == 2 * count


@pytest.mark.parametrize(
    "fault",
    [
        "missing-reversal",
        "swapped-reversal",
        "wrong-source",
        "shared-journal",
        "wrong-old-journal",
        "new-reuses-old",
        "retained-id",
        "retained-version",
        "new-version",
        "old-version",
        "positions",
        "missing-zero-proof",
        "zero-posting",
        "duplicate-source",
        "kind",
        "cancel-body",
        "nonconsecutive",
        "reason",
        "empty",
        "overflow",
    ],
)
def test_impossible_or_incomplete_receipt_is_rejected(fault):
    r = example()
    before, after = r["previous"]["lines"], r["replacement"]["lines"]
    if fault == "missing-reversal":
        r["reversals"].pop()
    if fault == "swapped-reversal":
        r["reversals"].reverse()
    if fault == "wrong-source":
        r["reversals"][0]["source_line_id"] = uuid4()
    if fault == "shared-journal":
        r["reversals"][0]["journal_id"] = after[0]["posting"]["journal_id"]
    if fault == "wrong-old-journal":
        after[0]["posting"]["journal_id"] = before[0]["posting"]["journal_id"]
    if fault == "new-reuses-old":
        after[1]["posting"]["operation_id"] = before[0]["posting"]["operation_id"]
    if fault == "retained-id":
        after[0]["posting"]["operation_id"] = uuid4()
    if fault == "retained-version":
        after[0]["posting"]["operation_version"] = 6
    if fault == "new-version":
        after[1]["posting"]["operation_version"] = 2
    if fault == "old-version":
        before[0]["posting"]["operation_version"] = r["previous_version"]
    if fault == "positions":
        after[1]["line_no"] = 10
    if fault == "missing-zero-proof":
        after[0]["posting"] = None
    if fault == "zero-posting":
        before[1]["posting"] = deepcopy(before[0]["posting"])
    if fault == "duplicate-source":
        after[0]["id"] = after[1]["id"]
    if fault == "kind":
        r["replacement"]["document_kind"] = "invoice"
    if fault == "cancel-body":
        r["action"] = "cancel"
    if fault == "nonconsecutive":
        r["version"] += 1
    if fault == "reason":
        r["reason"] = " "
    if fault == "empty":
        r["previous"]["lines"] = []
    if fault == "overflow":
        after[0]["net_amount"] = "9" * 21
    with pytest.raises(ValidationError):
        DocumentRevisionResponse.model_validate(r)


@pytest.mark.parametrize(
    "field", ["net_amount", "tax_amount", "total_amount", "unit_price", "asset"]
)
def test_exact_saved_prices_are_proved_against_the_actual_catalog(field):
    r = example()
    if field != "asset":
        r["replacement"]["lines"][0][field] = "0.01"
    result = DocumentRevisionResponse.model_validate(r)
    with pytest.raises(MoneyError):
        result.replacement.verify_prices(ASSETS["USD" if field == "asset" else "EUR"])


def test_hash_binds_full_original_json_action_source_ledger_and_omitted_values():
    doc, _ = current([5, 3])
    cmd = ConfirmedDocumentCorrection(
        expected_version=doc.version,
        reason="Fictional reviewed",
        replacement=proposed(doc, [(row.id, "2.00") for row in doc.lines]),
    )
    raw = cmd.model_dump(mode="json", exclude_unset=True)

    def digest(action, ledger, source, body):
        return document_revision_hash_v2(
            action, ledger, source, cmd, raw_body=json.dumps(body).encode()
        )

    key = digest("correct", doc.ledger_id, doc.id, raw)
    assert document_revision_hash_v2("correct", doc.ledger_id, doc.id, cmd) == key
    assert digest("correct", doc.ledger_id, doc.id, dict(reversed(raw.items()))) == key
    assert key != digest("correct", uuid4(), doc.id, raw)
    assert key != digest("correct", doc.ledger_id, uuid4(), raw)
    omitted = deepcopy(raw)
    omitted["replacement"].pop("notes", None)
    explicit = deepcopy(omitted)
    explicit["replacement"]["notes"] = None
    assert digest("correct", doc.ledger_id, doc.id, omitted) != digest(
        "correct", doc.ledger_id, doc.id, explicit
    )
    assert key != digest("cancel", doc.ledger_id, doc.id, raw)
    for change in ("unit_price", "order", "null", "reason"):
        altered = deepcopy(raw)
        if change == "unit_price":
            altered["replacement"]["lines"][0]["unit_price"] = "2.0"
        if change == "order":
            altered["replacement"]["lines"].reverse()
        if change == "null":
            altered["replacement"]["notes"] = (
                "Fictional new note" if altered["replacement"].get("notes") is None else None
            )
        if change == "reason":
            altered["reason"] = " Fictional reviewed "
        assert key != digest("correct", doc.ledger_id, doc.id, altered)
    cancel = ConfirmedDocumentCancellation(expected_version=3, reason="Fictional cancellation")
    assert document_revision_hash_v2("cancel", doc.ledger_id, doc.id, cancel) != key


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "omitted-zero-source",
        "foreign-ledger",
        "different-header",
        "different-amount-spelling",
        "stale-version",
        "wrong-original-journal",
        "missing-posting",
        "orphan-posting",
        "different-original-total",
        "different-original-tax",
    ],
)
def test_valid_shaped_receipt_is_bound_to_complete_old_and_submitted_sources(fault):
    doc, ops, body, r = sources_and_receipt()
    cmd = ConfirmedDocumentCorrection(
        expected_version=doc.version, reason=r["reason"], replacement=body
    )
    if fault == "omitted-zero-source":
        r["previous"]["lines"].pop(1)
    if fault == "foreign-ledger":
        r["ledger_id"] = uuid4()
    if fault == "different-header":
        r["replacement"]["notes"] = "Fictional other reviewed notes"
    if fault == "different-amount-spelling":
        r["replacement"]["lines"][0]["unit_price"] = "2"
    if fault == "stale-version":
        cmd = cmd.model_copy(update={"expected_version": doc.version - 1})
    if fault == "wrong-original-journal":
        ops = (replace(ops[0], journal_id=uuid4()), ops[1])
    if fault == "missing-posting":
        ops = ops[:1]
    if fault == "orphan-posting":
        ops = (
            *ops,
            replace(ops[0], source_line_id=uuid4(), journal_id=uuid4(), operation_id=uuid4()),
        )
    if fault == "different-original-total":
        doc = doc.model_copy(update={"total_amount": "70.00"})
    if fault == "different-original-tax":
        doc = doc.model_copy(
            update={
                "lines": [doc.lines[0].model_copy(update={"tax_amount": "1.23"}), *doc.lines[1:]]
            }
        )
    receipt = DocumentRevisionResponse.model_validate(r)
    if fault is None:
        verify_document_receipt_sources(receipt, doc, cmd, ops, ASSETS["USD"], ASSETS["EUR"])
        cancel = DocumentRevisionResponse.model_validate(
            r | {"action": "cancel", "replacement": None}
        )
        verify_document_receipt_sources(
            cancel,
            doc,
            ConfirmedDocumentCancellation(expected_version=doc.version, reason=r["reason"]),
            ops,
            ASSETS["USD"],
            None,
        )
    else:
        with pytest.raises(MoneyError) as err:
            verify_document_receipt_sources(receipt, doc, cmd, ops, ASSETS["USD"], ASSETS["EUR"])
        assert err.value.code == "document_revision_integrity"
