"""Commit the real return lifecycle and verify all fixed evidence across later changes."""

from uuid import uuid4

import pytest
from coinpup_api.business.allocation_return_models import BusinessAllocationReturnLine as Original
from coinpup_api.business.allocation_return_revision_member_models import (
    BusinessReturnRevisionMember as Member,
)
from coinpup_api.ledger.models import CommandReceipt, FinancialOperation, Journal
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_allocation_return_revision_receipts import validate
from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_credit_fact_validation import stored
from tests.integration.business.test_current_allocation_return_members import current
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_service import correction, submit
from tests.integration.business.test_refund_service import refund
from tests.integration.business.test_return_journal_transitions import group as group
from tests.integration.business.test_return_journal_transitions import stage_transition
from tests.integration.business.test_return_revision_refund_guard import example
from tests.integration.business.test_return_source_sequences import pair, slots
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def fixed(c, ledger, key):
    return c.scalar(
        select(CommandReceipt.response).where(
            CommandReceipt.ledger_id == ledger, CommandReceipt.key == key
        )
    )


def commit_chain(s, body, original, asset):
    setup = (s, body, original)
    sources = [m.source_allocation_line_id for m in original.postings]
    with s["engine"].begin() as c:
        before = fixed(c, s["ledger"], "fictional-return")
        first = stage_transition(c, setup, asset=asset)
        e2 = fixed(c, s["ledger"], first["idempotency_key"])
    with s["engine"].begin() as c:
        actual = current(c, s["ledger"], sources)
        assert [r["source_allocation_line_id"] for r in actual] == list(reversed(sources))
        assert all(r["group_version"] == 2 and r["operation_version"] == 2 for r in actual)
        for m in e2["members"]:
            for j in [m["reversal_journal_id"], m["replacement"]["journal_id"]]:
                assert c.scalar(select(Journal.sealed).where(Journal.id == j)) is True
        last = stage_transition(c, setup, asset=asset, cancel=True, prior=e2)
        e3 = fixed(c, s["ledger"], last["idempotency_key"])
    with s["engine"].connect() as c:
        assert current(c, s["ledger"], sources) == []
        assert before == fixed(c, s["ledger"], "fictional-return")
        assert e2 == fixed(c, s["ledger"], first["idempotency_key"])
        assert e3 == fixed(c, s["ledger"], last["idempotency_key"])
        assert len(e2["members"]) == len(e3["members"]) == len(sources)
        for h in [first, last]:
            validate(c, h["id"])
        for m in e3["members"]:
            assert m["replacement"] is None and m["operation_version"] == 3
            state = c.execute(
                select(FinancialOperation.status, FinancialOperation.current_journal_id).where(
                    FinancialOperation.id == m["operation_id"]
                )
            ).one()
            assert state[0] == "cancelled" and str(state[1]) == m["previous"]["journal_id"]
    before_replay = snapshot(s)
    assert submit_return(s, body) == original and snapshot(s) == before_replay


@pytest.mark.parametrize("group", [("USD", "receipt"), ("EUR", "payout")], indirect=True)
def test_complete_commit_correct_cancel_keeps_fixed_original_and_both_revisions(group):
    setup, asset = group
    commit_chain(*setup, asset)


def test_two_hundred_members_commit_and_replay_in_original_and_replacement_order(drafts):
    _, _, _, body = setup_return(drafts, direction="receipt", count=200)
    body = body.model_copy(
        update={
            "lines": [
                line.model_copy(update={"payment_amount": "10", "document_amount": "10"})
                for line in body.lines
            ]
        }
    )
    original = submit_return(drafts, body)
    assert len(original.postings) == 200
    commit_chain(drafts, body, original, "USD")


@pytest.mark.parametrize(
    "fault,constraint",
    [
        ("receipt", "ck_return_revision_shape"),
        ("missing", "ck_return_revision_shape"),
        ("reason", "ck_return_revision_shape"),
        ("date", "ck_journal_reversal"),
        ("sign", "ck_journal_reversal"),
        ("posting", "ck_business_allocation_return_shape"),
        ("owner", "ck_return_revision_shape"),
        ("dimensions", "ck_journal_reversal_dimensions"),
    ],
)
def test_broken_group_fails_before_its_first_commit_with_zero_changes(group, fault, constraint):
    setup, asset = group
    s, body, original = setup
    before = snapshot(s)
    with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
        stage_transition(c, setup, asset=asset, fault=fault)
        # Fire both explicit proofs before the generic operation history.
        c.exec_driver_sql(
            "SET CONSTRAINTS tr_business_return_revisions_complete,"
            "tr_journals_reversal_dimensions IMMEDIATE"
        )
    assert rejected.value.orig.diag.constraint_name == constraint
    assert snapshot(s) == before and submit_return(s, body) == original


@pytest.mark.parametrize("revised", [False, True])
def test_extra_member_cannot_join_a_group_after_its_complete_header_was_checked(drafts, revised):
    s = drafts
    _, _, _, body = setup_return(s, count=2)
    first, other = body.lines
    body = body.model_copy(
        update={
            "lines": [first.model_copy(update={"payment_amount": "10", "document_amount": "10"})]
        }
    )
    original = submit_return(s, body)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
        op = original.postings[0]
        later = c.execute(
            text("SELECT operation_id FROM business_allocation_lines WHERE id=:id"),
            {"id": other.source_allocation_line_id},
        ).scalar_one()
        if revised:
            h = stage_transition(c, (s, body, original))
            c.exec_driver_sql("SET CONSTRAINTS tr_business_return_revisions_complete IMMEDIATE")
            record = (
                c.execute(select(Member.__table__).where(Member.revision_id == h["id"]))
                .mappings()
                .one()
            )
            extra = dict(record) | dict(
                id=uuid4(),
                operation_id=uuid4(),
                source_operation_id=later,
                source_allocation_line_id=other.source_allocation_line_id,
                member_no=2,
                line_no=2,
                action="create",
                operation_version=1,
                previous_line_no=None,
                previous_operation_version=None,
                previous_journal_id=None,
                reversal_journal_id=None,
                journal_id=uuid4(),
            )
            birth = stored(c, FinancialOperation, op.operation_id) | dict(
                id=extra["operation_id"], version=1, current_journal_id=extra["journal_id"]
            )
            c.execute(insert(FinancialOperation).values(**birth))
            c.execute(insert(Member).values(**extra))
            c.exec_driver_sql("SET CONSTRAINTS tr_return_revision_members_complete IMMEDIATE")
        else:
            record = (
                c.execute(
                    select(Original.__table__).where(Original.operation_id == op.operation_id)
                )
                .mappings()
                .one()
            )
            extra = dict(record) | dict(
                id=uuid4(),
                operation_id=uuid4(),
                source_allocation_line_id=other.source_allocation_line_id,
                journal_id=uuid4(),
                line_no=2,
            )
            c.execute(insert(Original).values(**extra))
            c.exec_driver_sql(
                "SET CONSTRAINTS tr_business_allocation_return_lines_complete IMMEDIATE"
            )
    assert rejected.value.orig.diag.constraint_name == (
        "ck_return_revision_shape" if revised else "ck_business_allocation_return_shape"
    )
    assert snapshot(s) == before


@pytest.mark.parametrize("direction", ["receipt", "payout"])
def test_actual_cancel_requires_prior_explicit_release_by_every_refund(drafts, direction):
    s = drafts
    body, original, cash, first = example(s, direction)
    second_body = cash.model_copy(update={"id": uuid4()})
    second = refund(s, second_body, "fictional-second-linked")
    setup = (s, body, original)
    for index, receipt in enumerate([first, second]):
        before = snapshot(s)
        with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
            stage_transition(c, setup, cancel=True)
        assert rejected.value.orig.diag.constraint_name == "ck_return_revision_refund_reference"
        assert snapshot(s) == before
        intent = correction(s, receipt, amount="5", payment_amount="5", allocation_return_ids=[])
        edited = submit(s, receipt, "correct", intent, f"fictional-explicit-unlink-{index}")
        assert not edited.replacement.allocation_return_ids
    with s["engine"].begin() as c:
        h = stage_transition(c, setup, cancel=True)
    source = original.postings[0].source_allocation_line_id
    with s["engine"].connect() as c:
        assert not current(c, s["ledger"], [source])
        validate(c, h["id"])
    before = snapshot(s)
    assert submit_return(s, body) == original and refund(s, cash) == first
    assert refund(s, second_body, "fictional-second-linked") == second and snapshot(s) == before
    again = submit_return(s, body.model_copy(update={"id": uuid4()}), "fictional-return-again")
    with s["engine"].connect() as c:
        history = slots(c, s["ledger"], [source])
        assert [v["assigned_no"] for v in history] == [1, 2]
        assert history[-1]["operation_id"] == again.postings[0].operation_id
        assert history[-1]["predecessor"] == original.postings[0].operation_id


@pytest.mark.parametrize(
    "original_payment,last_payment,valid", [("0.05", "0.02", False), ("0.06", "0.03", True)]
)
def test_real_commit_preserves_later_same_source_rounding(
    drafts, original_payment, last_payment, valid
):
    s = drafts
    source, first, second, body = pair(s, original_payment, last_payment)
    before = snapshot(s)
    if valid:
        with s["engine"].begin() as c:
            h = stage_transition(c, (s, body, first), asset="EUR", cancel=True)
        with s["engine"].connect() as c:
            active = current(c, s["ledger"], [source])
            assert [m["operation_id"] for m in active] == [second.postings[0].operation_id]
            validate(c, h["id"])
    else:
        with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
            stage_transition(c, (s, body, first), asset="EUR", cancel=True)
        assert rejected.value.orig.diag.constraint_name == "ck_return_revision_source_history"
        assert snapshot(s) == before
    assert submit_return(s, body) == first
