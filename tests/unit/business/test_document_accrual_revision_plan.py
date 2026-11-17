"""Complete source transitions, including zero-priced facts and gaps in draft positions."""

from dataclasses import replace
from datetime import date
from decimal import localcontext
from uuid import uuid4

import pytest
from coinpup_api.business.confirmation_reads import BusinessConfirmedDocument
from coinpup_api.business.document_revision_plan import (
    SealedAccrual,
    plan_document_accrual_revision,
)
from coinpup_api.business.document_revision_schemas import ConfirmedDocumentReplacement
from coinpup_api.business.draft_schemas import BusinessDraftLineInput, DraftHeaderInput
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.errors import MoneyError

from tests.unit.business.test_confirmation_plan import draft


def current(values, kind="bill"):
    source = draft(kind)
    raw = source.model_dump()
    raw.update(
        state="confirmed", confirmation_id=uuid4(), confirmed_at=source.created_at, version=9
    )
    first = raw["lines"][0]
    raw["lines"] = [
        first
        | dict(
            id=uuid4(),
            line_no=2 + i * 3,
            quantity="1",
            unit_price=str(value),
            tax_rate_percent="0",
            net_amount=str(value),
            total_amount=str(value),
            tax_amount="0",
            recognition_date=date(2026, 9, i % 15 + 1),
        )
        for i, value in enumerate(values)
    ]
    raw.update(
        line_count=len(values),
        net_amount=str(sum(values)),
        total_amount=str(sum(values)),
        tax_amount="0",
    )
    doc = BusinessConfirmedDocument.model_validate(raw)
    postings = tuple(
        SealedAccrual(doc.ledger_id, doc.id, row.id, uuid4(), 3, uuid4())
        for row, value in zip(doc.lines, values, strict=True)
        if value
    )
    return doc, postings


def proposed(doc, specs, asset="EUR"):
    header = doc.model_dump(include=set(DraftHeaderInput.model_fields)) | dict(asset_id=asset)
    line = doc.lines[0].model_dump(include=set(BusinessDraftLineInput.model_fields))
    return ConfirmedDocumentReplacement(
        **header,
        lines=[
            line | dict(id=source_id, unit_price=value, recognition_date="2026-12-03")
            for source_id, value in specs
        ],
    )


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_complete_union_records_all_zero_transitions_and_never_reuses_retired_operations(kind):
    doc, ops = current([7, 0, 8, 0, 9, 0], kind)
    a, b, c, d, e, f = [row.id for row in doc.lines]
    g, h = uuid4(), uuid4()
    next_rows = proposed(
        doc, [(c, "0.00"), (h, "0"), (b, "0.01"), (a, "31.75"), (g, "2.19"), (d, "0")]
    )
    before = doc.model_dump()
    with localcontext() as ctx:
        ctx.prec = 2
        plan = plan_document_accrual_revision(doc, ASSETS["USD"], ops, next_rows, ASSETS["EUR"])
    assert doc.model_dump() == before
    assert (plan.previous_version, plan.version) == (9, 10)
    assert [v.source_line_id for v in plan.lines] == [c, h, b, a, g, d, e, f]
    assert [v.posting_action for v in plan.lines] == [
        "cancel",
        None,
        "create",
        "correct",
        "create",
        None,
        "cancel",
        None,
    ]
    assert [v.next_operation_version for v in plan.lines] == [4, None, 1, 4, 1, None, 4, None]
    assert [v.line_no for v in plan.lines] == [1, 2, 3, 4, 5, 6, None, None]
    assert [v.previous.line_no if v.previous else None for v in plan.lines] == [
        8,
        None,
        5,
        2,
        None,
        11,
        14,
        17,
    ]
    assert plan.lines[0].previous_posting == ops[1] and plan.lines[3].previous_posting == ops[0]
    assert plan.lines[2].previous_posting is None
    assert plan.lines[2].accrual.account_amount.minor_units == (1 if kind == "invoice" else -1)
    for line in plan.lines:
        if line.accrual is not None:
            assert line.accrual.account_amount.asset == ASSETS["EUR"]
            assert (line.accrual.account_amount + line.accrual.category_amount).minor_units == 0
    assert set(plan.affected_dates) == {
        doc.issue_date,
        date(2026, 12, 3),
        *(row.recognition_date for row in doc.lines),
    }


def test_full_200_line_swap_keeps_400_ordered_evidence_records_and_one_terminal_cancellation():
    doc, ops = current([1] * 200)
    new = proposed(doc, [(uuid4(), "1.00") for _ in range(200)])
    out = plan_document_accrual_revision(doc, ASSETS["USD"], ops, new, ASSETS["EUR"])
    assert [line.source_line_id for line in out.lines] == [line.id for line in new.lines] + [
        line.id for line in doc.lines
    ]
    assert [line.posting_action for line in out.lines] == ["create"] * 200 + ["cancel"] * 200
    old_disabled = replace(ASSETS["USD"], enabled=False)
    out = plan_document_accrual_revision(doc, old_disabled, ops, None, None)
    assert len(out.lines) == 200 and all(
        line.posting_action == "cancel" and line.accrual is None for line in out.lines
    )
    assert tuple(line.previous_posting for line in out.lines) == ops


@pytest.mark.parametrize(
    "fault",
    [
        "wrong-document",
        "wrong-ledger",
        "missing",
        "extra",
        "zero",
        "duplicate-op",
        "duplicate-journal",
        "version",
        "duplicate-line",
        "order",
        "total",
        "zero-amount",
        "line-kind",
        "exhausted",
    ],
)
def test_inconsistent_old_evidence_fails_before_creating_any_transition(fault):
    doc, ops = current([3, 4, 0])
    a, b = ops
    if fault == "wrong-document":
        ops = (replace(a, document_id=uuid4()), b)
    if fault == "wrong-ledger":
        ops = (replace(a, ledger_id=uuid4()), b)
    if fault == "missing":
        ops = (a,)
    if fault == "extra":
        ops = (*ops, a)
    if fault == "zero":
        ops = (a, replace(b, source_line_id=doc.lines[-1].id))
    if fault == "duplicate-op":
        ops = (a, replace(b, operation_id=a.operation_id))
    if fault == "duplicate-journal":
        ops = (a, replace(b, journal_id=a.journal_id))
    if fault == "version":
        ops = (replace(a, version=doc.version), b)
    if fault == "duplicate-line":
        doc = doc.model_copy(update={"lines": [doc.lines[0], doc.lines[0], doc.lines[2]]})
    if fault == "order":
        doc = doc.model_copy(update={"lines": list(reversed(doc.lines))})
    if fault == "total":
        doc = doc.model_copy(update={"net_amount": "6.99"})
    if fault == "zero-amount":
        doc = doc.model_copy(
            update={"lines": [*doc.lines[:2], doc.lines[2].model_copy(update={"net_amount": "3"})]}
        )
    if fault == "line-kind":
        doc = doc.model_copy(
            update={
                "lines": [
                    doc.lines[0].model_copy(update={"category_kind": "income"}),
                    *doc.lines[1:],
                ]
            }
        )
    if fault == "exhausted":
        doc = doc.model_copy(update={"version": 2147483647})
    with pytest.raises(MoneyError) as failure:
        plan_document_accrual_revision(doc, ASSETS["USD"], ops, None, None)
    assert failure.value.code == "document_revision_integrity"
