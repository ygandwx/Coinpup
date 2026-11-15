"""Prove original-ratio rounding in permanent source order, including after removal and re-add."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.allocation_return_revision_member_models import (
    BusinessReturnRevisionMember as Member,
)
from coinpup_api.business.allocation_return_revision_models import (
    BusinessAllocationReturnRevision as Revision,
)
from coinpup_api.business.allocation_return_schemas import BusinessAllocationReturnCreate
from coinpup_api.ledger.models import FinancialOperation
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_allocation_return_revision_headers import revision as revision
from tests.integration.business.test_allocation_return_revision_members import member
from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_allocation_return_storage import another_return, pending, rows
from tests.integration.business.test_credit_fact_validation import stored
from tests.integration.business.test_current_allocation_return_members import (
    current,
    stage_source_swap,
)
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import return_source_sequence_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def validate(c, ledger, identifiers):
    c.execute(
        text("SELECT coinpup_validate_return_source_sequences(:ledger,CAST(:ids AS uuid[]))"),
        {"ledger": ledger, "ids": identifiers},
    )


def slots(c, ledger, sources):
    return (
        c.execute(
            text(
                "SELECT * FROM coinpup_return_source_slots(:ledger,CAST(:ids AS uuid[])) "
                "ORDER BY source_id,assigned_no"
            ),
            {"ledger": ledger, "ids": sources},
        )
        .mappings()
        .all()
    )


def pair(s, source_payment="0.05", terminal_payment="0.02"):
    values = rows(
        s,
        document_asset="EUR",
        source_payment_amount=source_payment,
        source_document_amount="0.04",
        return_payment_amount="0.03",
        return_document_amount="0.02",
    )
    source_id = values["member"]["source_allocation_line_id"]
    base = {
        "source_allocation_line_id": source_id,
        "expected_version": 1,
        "payment_amount": "0.03",
        "document_amount": "0.02",
    }
    body = BusinessAllocationReturnCreate(
        id=uuid4(),
        payment_id=values["group"]["payment_id"],
        return_date=date(2026, 10, 1),
        reason="Fictional source order",
        lines=[base],
    )
    second_body = BusinessAllocationReturnCreate.model_validate(
        body.model_dump() | {"id": uuid4(), "lines": [base | {"payment_amount": terminal_payment}]}
    )
    first = submit_return(s, body)
    second = submit_return(s, second_body, "fictional-later-return")
    return source_id, first, second, body


def cancel_first(c, s, first):
    header = dict(
        id=uuid4(),
        return_id=first.id,
        payment_id=first.payment_id,
        ledger_id=s["ledger"],
        group_version=2,
        action="cancel",
        revision_reason="Fictional remove first share",
        created_by=s["owner"],
        idempotency_key="fictional-sequence-cancel",
    )
    c.execute(insert(Revision).values(**header))
    row = member(header, first) | dict(
        action="cancel",
        line_no=None,
        journal_id=None,
        payment_amount=None,
        document_amount=None,
        absorbs_remainder=None,
    )
    c.execute(insert(Member).values(**row))


@pytest.mark.parametrize(
    "source_p,last_p,allowed", [("0.05", "0.02", False), ("0.06", "0.03", True)]
)
def test_early_cancellation_revalues_the_later_saved_final_share(drafts, source_p, last_p, allowed):
    s = drafts
    source_id, first, second, body = pair(s, source_p, last_p)
    before = snapshot(s)
    with s["engine"].connect() as c:
        validate(c, s["ledger"], [source_id, source_id])
        initial = slots(c, s["ledger"], [source_id])
        assert [r["assigned_no"] for r in initial] == [1, 2]
        assert [r["operation_id"] for r in initial] == [
            first.postings[0].operation_id,
            second.postings[0].operation_id,
        ]
        assert [r["predecessor"] for r in initial] == [None, first.postings[0].operation_id]
        assert initial[0]["birth_seq"] < initial[1]["birth_seq"]
        cancel_first(c, s, first)
        assert slots(c, s["ledger"], [source_id]) == initial
        if allowed:
            validate(c, s["ledger"], [source_id])
        else:
            with pytest.raises(IntegrityError) as rejected:
                validate(c, s["ledger"], [source_id])
            assert rejected.value.orig.diag.constraint_name == "ck_return_revision_source_sequence"
        c.rollback()
    assert submit_return(s, body) == first and snapshot(s) == before


@pytest.mark.parametrize(
    "fault", ["gap", "missing-source", "foreign-ledger", "null", "empty", "duplicate-event"]
)
def test_unknown_or_falsely_reserved_source_sequence_fails_without_writes(revision, fault):
    s, _, _, first = revision
    orig = first.postings[0]
    before = snapshot(s)
    with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
        sources = [orig.source_allocation_line_id]
        ledger = s["ledger"]
        if fault == "gap":
            h = dict(
                id=uuid4(),
                return_id=first.id,
                payment_id=first.payment_id,
                ledger_id=ledger,
                group_version=2,
                action="correct",
                revision_reason="Fictional reason",
                return_date=date(2026, 11, 8),
                return_reason="Fictional re-add",
                created_by=s["owner"],
                idempotency_key="fictional-sequence-gap",
            )
            c.execute(insert(Revision).values(**h))
            row = member(h, first) | dict(
                action="create",
                operation_id=uuid4(),
                operation_version=1,
                source_return_no=3,
                source_predecessor_operation_id=orig.operation_id,
                previous_line_no=None,
                previous_operation_version=None,
                previous_journal_id=None,
                reversal_journal_id=None,
            )
            born = stored(c, FinancialOperation, orig.operation_id) | dict(
                id=row["operation_id"], current_journal_id=row["journal_id"]
            )
            c.execute(insert(FinancialOperation).values(**born))
            c.execute(insert(Member).values(**row))
        elif fault == "duplicate-event":
            seq = slots(c, ledger, sources)[0]["birth_seq"]
            old = dict(
                c.execute(text("SELECT * FROM change_log WHERE seq=:s"), {"s": seq})
                .mappings()
                .one()
            )
            old.pop("seq")
            c.execute(insert(ChangeLog).values(**old))
        elif fault == "foreign-ledger":
            ledger = uuid4()
        elif fault == "missing-source":
            sources = [uuid4()]
        elif fault == "empty":
            sources = []
        else:
            sources = None
        validate(c, ledger, sources)
    expected = (
        "ck_change_log_source"
        if fault == "duplicate-event"
        else "ck_return_revision_source_sequence"
    )
    assert rejected.value.orig.diag.constraint_name == expected
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "wrong", ["other-operation", "changed-position", "subminor", "over", "one-sided", "wrong-ratio"]
)
def test_retained_member_must_own_its_permanent_position_and_match_both_assets(revision, wrong):
    s, h, body, first = revision
    other = submit_return(s, body.model_copy(update={"id": uuid4()}), "fictional-another-slot")
    other_op = other.postings[0].operation_id
    row = member(h, first)
    if wrong == "other-operation":
        row["operation_id"] = other_op
    elif wrong == "changed-position":
        row.update(source_return_no=2, source_predecessor_operation_id=other_op)
    elif wrong == "subminor":
        row.update(payment_amount=Decimal("0.001"), document_amount=Decimal("0.001"))
    elif wrong == "over":
        row.update(payment_amount=Decimal("100.01"), document_amount=Decimal("100.01"))
    elif wrong == "one-sided":
        row.update(payment_amount=Decimal("100"), document_amount=Decimal("99.99"))
    else:
        row.update(payment_amount=Decimal("12"), document_amount=Decimal("11.99"))
    before = snapshot(s)
    with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**h))
        c.execute(insert(Member).values(**row))
        validate(c, s["ledger"], [first.postings[0].source_allocation_line_id])
    assert rejected.value.orig.diag.constraint_name == "ck_return_revision_source_sequence"
    assert snapshot(s) == before


def test_backdated_second_group_is_still_the_second_source_slot(drafts):
    s = drafts
    v = rows(
        s,
        document_asset="EUR",
        source_payment_amount="0.06",
        source_document_amount="0.04",
        return_payment_amount="0.03",
        return_document_amount="0.02",
    )
    v["group"]["return_date"] = v["journal"]["transaction_date"] = v["journal"][
        "recognition_date"
    ] = date(2026, 11, 1)
    with s["engine"].begin() as c:
        pending(c, v)
    nxt = another_return(v, payment_amount="0.03", document_amount="0.02", source_return_no=2)
    nxt["group"]["return_date"] = nxt["journal"]["transaction_date"] = nxt["journal"][
        "recognition_date"
    ] = date(2026, 10, 1)
    with s["engine"].begin() as c:
        pending(c, nxt)
    before = snapshot(s)
    with s["engine"].connect() as c:
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        sources = [v["member"]["source_allocation_line_id"]]
        validate(c, s["ledger"], sources)
        assert [r["operation_id"] for r in slots(c, s["ledger"], sources)] == [
            v["operation"]["id"],
            nxt["operation"]["id"],
        ]
    assert snapshot(s) == before


def test_readding_a_removed_source_has_a_new_operation_and_cannot_reuse_the_original_slot(drafts):
    s = drafts
    _, _, _, body = setup_return(s, count=2)
    original = submit_return(s, body.model_copy(update={"lines": body.lines[:1]}))
    old = original.postings[0]
    new_source = body.lines[1].source_allocation_line_id
    sources = [old.source_allocation_line_id, new_source]
    before = snapshot(s)
    with s["engine"].connect() as c:
        header, newer, _ = stage_source_swap(c, s, original, new_source)
        validate(c, s["ledger"], sources)
        assert current(c, s["ledger"], sources[:1]) == []
        next_header = header | dict(
            id=uuid4(),
            group_version=3,
            previous_revision_id=header["id"],
            idempotency_key="fictional-readd",
        )
        c.execute(insert(Revision).values(**next_header))
        fresh = member(next_header, original) | dict(
            action="create",
            operation_id=uuid4(),
            operation_version=1,
            previous_line_no=None,
            previous_operation_version=None,
            previous_journal_id=None,
            reversal_journal_id=None,
            source_return_no=2,
            source_predecessor_operation_id=old.operation_id,
        )
        op = stored(c, FinancialOperation, old.operation_id) | dict(
            id=fresh["operation_id"], current_journal_id=fresh["journal_id"]
        )
        c.execute(insert(FinancialOperation).values(**op))
        removed = newer | dict(
            id=uuid4(),
            revision_id=next_header["id"],
            member_no=2,
            action="cancel",
            previous_line_no=1,
            previous_operation_version=1,
            previous_journal_id=newer["journal_id"],
            reversal_journal_id=uuid4(),
            operation_version=2,
            line_no=None,
            journal_id=None,
            payment_amount=None,
            document_amount=None,
            absorbs_remainder=None,
        )
        c.execute(insert(Member).values(**fresh))
        c.execute(insert(Member).values(**removed))
        validate(c, s["ledger"], sources)
        actual = slots(c, s["ledger"], sources[:1])
        assert [(r["assigned_no"], r["predecessor"], r["operation_id"]) for r in actual] == [
            (1, None, old.operation_id),
            (2, old.operation_id, fresh["operation_id"]),
        ]
        assert [r["operation_id"] for r in current(c, s["ledger"], sources)] == [
            fresh["operation_id"]
        ]
        c.rollback()
    assert snapshot(s) == before


def test_index_and_scoped_proof_migration_roundtrip_and_refusal_with_history(revision):
    s, header, _, first = revision
    before = snapshot(s)
    migrations = return_source_sequence_migration()
    index = "public.ix_change_log_return_slot_birth"
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        previous = c.scalar(text("SELECT pg_get_indexdef(to_regclass(:i))"), {"i": index})
        assert "INCLUDE (seq)" in previous and "entity_version = 1" in previous
        migrations["downgrade"]()
        assert c.scalar(text("SELECT to_regclass(:i)"), {"i": index}) is None
        migrations["upgrade"]()
        assert c.scalar(text("SELECT pg_get_indexdef(to_regclass(:i))"), {"i": index}) == previous
        validate(c, s["ledger"], [first.postings[0].source_allocation_line_id])
    with pytest.raises(IntegrityError) as err, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**header))
        with Operations.context(MigrationContext.configure(c)):
            migrations["downgrade"]()
    assert err.value.orig.diag.constraint_name == "ck_return_revision_downgrade"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "quantities,same_asset,valid",
    [
        ((5, 4, 0, 0, 3, 2), False, True),
        ((5, 4, 3, 2, 2, 2), False, True),
        ((5, 4, 0, 0, 2, 2), False, False),
        ((0, 0, 0, 0, 1, 1), True, False),
        ((1, 2, 0, 0, 1, 1), False, False),
        ((9, 9, 3, 2, 1, 1), True, False),
        ((10, 10, Decimal("0.5"), Decimal("0.5"), 1, 1), True, False),
        (((10**38 - 1), 3, 0, 0, (10**38 - 1) // 3, 1), False, True),
        (((10**38 - 1), 3, (10**38 - 1) // 3, 1, 2 * ((10**38 - 1) // 3), 2), False, True),
    ],
)
def test_shared_ratio_leaf_compares_integer_units_without_losing_maximum_precision(
    structure_database, quantities, same_asset, valid
):
    engine, _ = structure_database
    with engine.connect() as c:
        answer = c.exec_driver_sql(
            "SELECT coinpup_return_share_valid(%s,%s,%s,%s,%s,%s,%s)", (*quantities, same_asset)
        ).scalar_one()
    assert answer is valid
