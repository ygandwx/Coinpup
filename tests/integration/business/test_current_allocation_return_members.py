"""Current return projection from real original postings and rollback-only staged revisions."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from coinpup_api.business.allocation_return_revision_member_models import (
    BusinessReturnRevisionMember as Member,
)
from coinpup_api.business.allocation_return_revision_models import (
    BusinessAllocationReturnRevision as Revision,
)
from coinpup_api.business.settlement_models import BusinessAllocationLine
from coinpup_api.ledger.models import FinancialOperation
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_allocation_return_revision_headers import revision as revision
from tests.integration.business.test_allocation_return_revision_members import member
from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def current(c, ledger, sources):
    return (
        c.execute(
            text(
                "SELECT * FROM public.coinpup_current_return_members(:ledger, "
                "CAST(:sources AS uuid[])) ORDER BY return_id,line_no"
            ),
            {"ledger": ledger, "sources": sources},
        )
        .mappings()
        .all()
    )


@pytest.mark.parametrize("direction,count", [("receipt", 1), ("payout", 2), ("receipt", 200)])
def test_real_originals_have_exact_scoped_quantity_order_and_identities(drafts, direction, count):
    s = drafts
    _, _, _, body = setup_return(s, direction=direction, count=count)
    original = submit_return(s, body)
    sources = [item.source_allocation_line_id for item in original.postings]
    before = snapshot(s)
    with s["engine"].connect() as c:
        rows = current(c, s["ledger"], sources + sources[:1] + [uuid4()])
        assert len(rows) == count
        for row, old in zip(rows, original.postings, strict=True):
            assert (
                row["ledger_id"],
                row["payment_id"],
                row["return_id"],
                row["revision_id"],
                row["group_version"],
                row["return_date"],
            ) == (s["ledger"], original.payment_id, original.id, None, 1, original.return_date)
            assert (
                row["source_allocation_line_id"],
                row["source_return_no"],
                row["line_no"],
                row["operation_id"],
                row["operation_version"],
                row["journal_id"],
            ) == (
                old.source_allocation_line_id,
                1,
                old.line_no,
                old.operation_id,
                1,
                old.journal_id,
            )
            assert row["payment_amount"] == Decimal(old.payment_amount) and row[
                "document_amount"
            ] == Decimal(old.document_amount)
        assert [r["line_no"] for r in current(c, s["ledger"], sources[-1:])] == [count]
        assert current(c, uuid4(), sources) == [] and current(c, s["ledger"], [uuid4()]) == []
        assert current(c, s["ledger"], []) == [] and current(c, s["ledger"], None) == []
    assert submit_return(s, body) == original and snapshot(s) == before


@pytest.mark.parametrize("action", ["correct", "cancel"])
def test_pending_effective_group_replaces_original_view_until_rollback(revision, action):
    s, header, body, original = revision
    source = original.postings[0].source_allocation_line_id
    row = member(header, original) | {
        "payment_amount": Decimal("0.01"),
        "document_amount": Decimal("0.01"),
    }
    if action == "cancel":
        header.update(action="cancel", return_date=None, return_reason=None)
        row.update(
            action="cancel",
            line_no=None,
            journal_id=None,
            payment_amount=None,
            document_amount=None,
            absorbs_remainder=None,
        )
    before = snapshot(s)
    with s["engine"].connect() as c:
        c.execute(insert(Revision).values(**header))
        c.execute(insert(Member).values(**row))
        found = current(c, s["ledger"], [source])
        if action == "correct":
            assert len(found) == 1
            assert (
                found[0]["group_version"],
                found[0]["revision_id"],
                found[0]["operation_version"],
                found[0]["journal_id"],
                found[0]["return_date"],
                found[0]["payment_amount"],
            ) == (2, header["id"], 2, row["journal_id"], header["return_date"], Decimal("0.01"))
        else:
            assert found == []
        with pytest.raises(IntegrityError) as caught:
            c.exec_driver_sql("SET CONSTRAINTS tr_business_return_revisions_complete IMMEDIATE")
        assert caught.value.orig.diag.constraint_name == "ck_return_revision_shape"
    with s["engine"].connect() as c:
        assert current(c, s["ledger"], [source])[0]["journal_id"] == original.postings[0].journal_id
    assert submit_return(s, body) == original and snapshot(s) == before


def test_two_original_groups_on_same_source_only_replace_the_selected_group(revision):
    s, header, body, original = revision
    other = submit_return(
        s, body.model_copy(update={"id": uuid4()}), "fictional-second-return-slot"
    )
    source = original.postings[0].source_allocation_line_id
    before = snapshot(s)
    with s["engine"].connect() as c:
        c.execute(insert(Revision).values(**header))
        first = member(header, original)
        c.execute(insert(Member).values(**first))
        assert {
            (r["return_id"], r["group_version"], r["source_return_no"], r["payment_amount"])
            for r in current(c, s["ledger"], [source])
        } == {(original.id, 2, 1, Decimal("12.34")), (other.id, 1, 2, Decimal("20"))}
        last = header | dict(
            id=uuid4(),
            group_version=3,
            previous_revision_id=header["id"],
            action="cancel",
            return_date=None,
            return_reason=None,
            idempotency_key="fictional-second-return-slot",
        )
        c.execute(insert(Revision).values(**last))
        c.execute(
            insert(Member).values(
                **(
                    first
                    | dict(
                        id=uuid4(),
                        revision_id=last["id"],
                        action="cancel",
                        previous_operation_version=2,
                        operation_version=3,
                        previous_journal_id=first["journal_id"],
                        reversal_journal_id=uuid4(),
                        line_no=None,
                        journal_id=None,
                        payment_amount=None,
                        document_amount=None,
                        absorbs_remainder=None,
                    )
                )
            )
        )
        only = current(c, s["ledger"], [source])
        assert [(r["return_id"], r["journal_id"]) for r in only] == [
            (other.id, other.postings[0].journal_id)
        ]
        c.rollback()
    assert snapshot(s) == before


def test_source_first_added_in_a_revision_is_discoverable_without_any_original_return_line(drafts):
    s = drafts
    _, _, _, body = setup_return(s, count=2)
    original = submit_return(s, body.model_copy(update={"lines": body.lines[:1]}))
    original_source = original.postings[0].source_allocation_line_id
    new_source = body.lines[1].source_allocation_line_id
    new_op, new_journal = uuid4(), uuid4()
    rid = uuid4()
    header = dict(
        id=rid,
        return_id=original.id,
        payment_id=original.payment_id,
        ledger_id=s["ledger"],
        group_version=2,
        previous_revision_id=None,
        action="correct",
        revision_reason="Fictional change source",
        return_date=date(2026, 11, 4),
        return_reason="Fictional replacement",
        created_by=s["owner"],
        idempotency_key="fictional-return",
    )
    old = member(header, original)
    before = snapshot(s)
    with s["engine"].connect() as c:
        original_op = c.scalar(
            select(BusinessAllocationLine.operation_id).where(
                BusinessAllocationLine.id == new_source
            )
        )
        assert current(c, s["ledger"], [new_source]) == []
        c.execute(insert(Revision).values(**header))
        c.execute(
            insert(FinancialOperation).values(
                id=new_op,
                ledger_id=s["ledger"],
                kind="unallocation",
                created_by=s["owner"],
                current_journal_id=new_journal,
            )
        )
        new = old | dict(
            id=uuid4(),
            action="create",
            source_allocation_line_id=new_source,
            source_operation_id=original_op,
            operation_id=new_op,
            operation_version=1,
            previous_line_no=None,
            previous_operation_version=None,
            previous_journal_id=None,
            reversal_journal_id=None,
            journal_id=new_journal,
        )
        removed = old | dict(
            action="cancel",
            member_no=2,
            line_no=None,
            journal_id=None,
            payment_amount=None,
            document_amount=None,
            absorbs_remainder=None,
        )
        c.execute(insert(Member).values(**new))
        c.execute(insert(Member).values(**removed))
        visible = current(c, s["ledger"], [new_source])
        assert [
            (
                x["source_allocation_line_id"],
                x["return_id"],
                x["revision_id"],
                x["operation_id"],
                x["journal_id"],
            )
            for x in visible
        ] == [(new_source, original.id, rid, new_op, new_journal)]
        assert (
            current(c, s["ledger"], [original_source]) == []
            and current(c, uuid4(), [new_source]) == []
        )
        c.rollback()
    assert snapshot(s) == before
