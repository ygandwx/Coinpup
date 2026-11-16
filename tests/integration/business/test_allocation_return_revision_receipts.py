"""Verify permanent 200 JSON against independently assembled immutable return group facts."""

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
from coinpup_api.business.allocation_return_revision_schemas import (
    AllocationReturnRevisionResponse,
    validate_return_revision_amounts,
)
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.models import CommandReceipt
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_allocation_return_revision_members import member
from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_current_allocation_return_members import stage_source_swap
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import return_group_receipt_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def response(c, identifier):
    return c.scalar(
        text("SELECT coinpup_business_return_revision_response(:id)"), {"id": identifier}
    )


def validate(c, identifier):
    c.execute(text("SELECT coinpup_validate_return_revision_receipt(:id)"), {"id": identifier})


def amount(value):
    return format(Decimal(value), ".18f")


def create_group(c, setup, *, asset="USD", prior=None, cancel=False, member_changes=None):
    s, body, original = setup
    reason = "Fictional cancel" if cancel else "Fictional correction"
    day = date(2026, 11, 8)
    h = dict(
        id=uuid4(),
        return_id=original.id,
        payment_id=original.payment_id,
        ledger_id=s["ledger"],
        group_version=2 if prior is None else prior["version"] + 1,
        previous_revision_id=None if prior is None else prior["revision_id"],
        action="cancel" if cancel else "correct",
        revision_reason=reason,
        return_date=None if cancel else day,
        return_reason=None if cancel else "Fictional new group 中文",
        created_by=s["owner"],
        idempotency_key="fictional-return-200-" + str(uuid4()),
    )
    c.execute(insert(Revision).values(**h))
    previous_group = (
        {"return_date": str(original.return_date), "reason": body.reason}
        if prior is None
        else prior["replacement"]
    )
    next_group = None if cancel else {"return_date": str(day), "reason": h["return_reason"]}
    expected = dict(
        kind="business_allocation_return_revision",
        return_id=str(original.id),
        revision_id=str(h["id"]),
        ledger_id=str(s["ledger"]),
        payment_id=str(original.payment_id),
        payment_asset_id="USD",
        previous_version=h["group_version"] - 1,
        version=h["group_version"],
        action=h["action"],
        reason=reason,
        previous=previous_group,
        replacement=next_group,
        members=[],
    )
    old_by_source = (
        {}
        if prior is None
        else {m["source_allocation_line_id"]: m["replacement"] for m in prior["members"]}
    )
    postings = list(original.postings)
    if not cancel:
        postings.reverse()
    elif prior:
        postings.sort(key=lambda f: old_by_source[str(f.source_allocation_line_id)]["line_no"])
    rows = []
    for i, f in enumerate(postings, 1):
        prev = (
            dict(
                line_no=f.line_no,
                operation_version=1,
                journal_id=str(f.journal_id),
                payment_amount=amount(f.payment_amount),
                document_amount=amount(f.document_amount),
            )
            if prior is None
            else old_by_source[str(f.source_allocation_line_id)]
        )
        row = member(h, original) | dict(
            member_no=i,
            action="cancel" if cancel else "correct",
            source_allocation_line_id=f.source_allocation_line_id,
            source_operation_id=f.source_operation_id,
            operation_id=f.operation_id,
            operation_version=prev["operation_version"] + 1,
            previous_line_no=prev["line_no"],
            previous_operation_version=prev["operation_version"],
            previous_journal_id=prev["journal_id"],
            line_no=None if cancel else i,
            journal_id=None if cancel else uuid4(),
            payment_amount=None if cancel else Decimal("11" if asset == "EUR" else "10"),
            document_amount=None if cancel else Decimal("10"),
            absorbs_remainder=None if cancel else False,
        )
        if member_changes:
            row.update(member_changes(i, rows))
        rows.append(row)
        following = (
            None
            if cancel
            else dict(
                line_no=i,
                operation_version=row["operation_version"],
                journal_id=str(row["journal_id"]),
                payment_amount=amount(row["payment_amount"]),
                document_amount=amount(row["document_amount"]),
            )
        )
        expected["members"].append(
            dict(
                action=row["action"],
                source_allocation_line_id=str(f.source_allocation_line_id),
                source_operation_id=str(f.source_operation_id),
                source_version=1,
                source_return_no=1,
                document_id=str(f.document_id),
                document_line_id=str(f.document_line_id),
                document_asset_id=asset,
                operation_id=str(f.operation_id),
                operation_version=row["operation_version"],
                previous=prev,
                reversal_journal_id=str(row["reversal_journal_id"]),
                replacement=following,
            )
        )
    c.execute(insert(Member), rows)
    return h, expected


def store_receipt(c, h, expected, *, overrides=None):
    c.execute(
        insert(CommandReceipt).values(
            **(
                dict(
                    ledger_id=h["ledger_id"],
                    key=h["idempotency_key"],
                    hash_version=2,
                    request_hash="a" * 64,
                    response_status=200,
                    response=expected,
                    operation_id=expected["members"][0]["operation_id"],
                )
                | (overrides or {})
            )
        )
    )


@pytest.mark.parametrize("direction,asset", [("receipt", "USD"), ("payout", "EUR")])
def test_exact_fixed_200_survives_later_cancel_and_the_original_201_is_unchanged(
    drafts, direction, asset
):
    s = drafts
    _, _, _, body = setup_return(s, direction=direction, asset=asset, count=2)
    original = submit_return(s, body)
    setup = (s, body, original)
    before = snapshot(s)
    with s["engine"].connect() as c:
        first, e2 = create_group(c, setup, asset=asset)
        store_receipt(c, first, e2)
        second, e3 = create_group(c, setup, asset=asset, prior=e2, cancel=True)
        store_receipt(c, second, e3)
        for style, header, expected in [
            ("German, DMY", first, e2),
            ("SQL, MDY", second, e3),
            ("ISO, YMD", first, e2),
        ]:
            c.execute(text("SELECT set_config('DateStyle', :style, true)"), {"style": style})
            actual = response(c, header["id"])
            assert actual == expected
            typed = validate_return_revision_amounts(
                AllocationReturnRevisionResponse.model_validate(actual), ASSETS
            )
            assert typed.model_dump(mode="json") == expected
            validate(c, header["id"])
        assert e2["members"][0]["previous"]["line_no"] == 2
        with pytest.raises(IntegrityError) as rejected:
            c.exec_driver_sql("SET CONSTRAINTS tr_business_return_revisions_complete IMMEDIATE")
        assert rejected.value.orig.diag.constraint_name == "ck_return_revision_shape"
        c.rollback()
    assert submit_return(s, body) == original and snapshot(s) == before


@pytest.mark.parametrize(
    "fault", ["anchor", "status", "hash", "missing", "amount", "type", "extra", "order"]
)
def test_another_receipt_cannot_prove_200_even_when_the_set_of_journals_is_unchanged(drafts, fault):
    s = drafts
    _, _, _, body = setup_return(s, count=2)
    original = submit_return(s, body)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
        h, expected = create_group(c, (s, body, original))
        changes = {}
        if fault == "anchor":
            changes["operation_id"] = expected["members"][1]["operation_id"]
        elif fault == "status":
            changes["response_status"] = 201
        elif fault == "hash":
            changes["hash_version"] = 1
        elif fault == "amount":
            expected["members"][0]["replacement"]["payment_amount"] = "10"
        elif fault == "type":
            expected["version"] = 2.0
        elif fault == "extra":
            expected["created_by"] = str(s["owner"])
        elif fault == "order":
            expected["members"].reverse()
        if fault != "missing":
            store_receipt(c, h, expected, overrides=changes)
        validate(c, h["id"])
    assert rejected.value.orig.diag.constraint_name == "ck_return_revision_shape"
    assert snapshot(s) == before


@pytest.mark.parametrize("fault", ["previous-version", "foreign-journal", "gap", "overlap"])
def test_cannot_construct_a_receipt_with_wrong_immutable_predecessors_or_member_order(
    drafts, fault
):
    s = drafts
    _, _, _, body = setup_return(s, count=2)
    original = submit_return(s, body)
    before = snapshot(s)

    def change(i, rows):
        if i != 2:
            return {}
        return {
            "previous-version": {"previous_operation_version": 2, "operation_version": 3},
            "foreign-journal": {"previous_journal_id": uuid4()},
            "gap": {"member_no": 3},
            "overlap": {"reversal_journal_id": rows[0]["journal_id"]},
        }[fault]

    with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
        h, _ = create_group(c, (s, body, original), member_changes=change)
        response(c, h["id"])
    assert rejected.value.orig.diag.constraint_name == "ck_return_revision_shape"
    assert snapshot(s) == before


def test_disjoint_replacement_names_the_new_v1_and_the_complete_retired_original_separately(drafts):
    s = drafts
    _, _, _, body = setup_return(s, count=2)
    cmd = body.model_copy(update={"lines": body.lines[:1]})
    orig = submit_return(s, cmd)
    old = orig.postings[0]
    new_source = body.lines[1].source_allocation_line_id
    before = snapshot(s)
    with s["engine"].connect() as c:
        header, added, removed = stage_source_swap(c, s, orig, new_source)
        parsed = validate_return_revision_amounts(
            AllocationReturnRevisionResponse.model_validate(response(c, header["id"])), ASSETS
        )
        born, retired = parsed.members
        assert (
            born.action,
            born.source_allocation_line_id,
            born.operation_id,
            born.operation_version,
            born.previous,
            born.reversal_journal_id,
        ) == ("create", new_source, added["operation_id"], 1, None, None)
        assert born.replacement.journal_id == added["journal_id"]
        assert (
            retired.action,
            retired.source_allocation_line_id,
            retired.operation_id,
            retired.previous.journal_id,
            retired.reversal_journal_id,
            retired.replacement,
        ) == (
            "cancel",
            old.source_allocation_line_id,
            old.operation_id,
            old.journal_id,
            removed["reversal_journal_id"],
            None,
        )
        c.rollback()
    assert submit_return(s, cmd) == orig and snapshot(s) == before


def test_exact_receipt_migration_roundtrip_and_protected_downgrade(drafts):
    s = drafts
    _, _, _, body = setup_return(s)
    orig = submit_return(s, body)
    before = snapshot(s)
    migration = return_group_receipt_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        live = definitions(c)
        migration["downgrade"]()
        assert (
            c.scalar(
                text("SELECT to_regprocedure('coinpup_business_return_revision_response(uuid)')")
            )
            is None
        )
        migration["upgrade"]()
        assert definitions(c) == live
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        h, expected = create_group(c, (s, body, orig))
        store_receipt(c, h, expected)
        with Operations.context(MigrationContext.configure(c)):
            migration["downgrade"]()
    assert caught.value.orig.diag.constraint_name == "ck_return_revision_downgrade"
    assert snapshot(s) == before


def test_complete_two_hundred_member_receipt_keeps_every_previous_and_replacement_in_order(drafts):
    s = drafts
    _, _, _, body = setup_return(s, count=200)
    orig = submit_return(s, body)
    before = snapshot(s)
    with s["engine"].connect() as c:
        header, expected = create_group(c, (s, body, orig))
        actual = response(c, header["id"])
        assert actual == expected
        parsed = validate_return_revision_amounts(
            AllocationReturnRevisionResponse.model_validate(actual), ASSETS
        )
        assert len(parsed.members) == 200
        assert [m.previous.line_no for m in parsed.members] == list(range(200, 0, -1))
        assert [m.replacement.line_no for m in parsed.members] == list(range(1, 201))
        store_receipt(c, header, expected)
        validate(c, header["id"])
        c.rollback()
    assert submit_return(s, body) == orig and snapshot(s) == before


@pytest.mark.parametrize("selected", [0, 1])
def test_projection_cannot_omit_the_rest_of_a_real_previous_group(drafts, selected):
    s = drafts
    _, _, _, body = setup_return(s, count=2)
    orig = submit_return(s, body)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        short = orig.model_copy(update={"postings": orig.postings[:selected]})
        if selected:
            h, _ = create_group(c, (s, body, short))
        else:
            h = dict(
                id=uuid4(),
                return_id=orig.id,
                payment_id=orig.payment_id,
                group_version=2,
                ledger_id=s["ledger"],
                created_by=s["owner"],
                action="cancel",
                revision_reason="Fictional missing members",
                idempotency_key="fictional-missing",
            )
            c.execute(insert(Revision).values(**h))
        response(c, h["id"])
    assert caught.value.orig.diag.constraint_name == "ck_return_revision_shape"
    assert snapshot(s) == before
