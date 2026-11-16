"""A later change to an earlier return must never alter the proof of a historical prefix."""

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
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_allocation_return_revision_members import member
from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_return_source_sequences import pair, validate
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def prove(c, ledger, group, revision=None):
    c.execute(
        text("SELECT coinpup_validate_return_source_history(:ledger,:group,:revision)"),
        {"ledger": ledger, "group": group, "revision": revision},
    )


def correction(
    c,
    s,
    original,
    *,
    payment,
    document,
    number=1,
    predecessor=None,
    prefix=("0", "0"),
    absorbs=False,
    previous=None,
    changes=None,
):
    h = dict(
        id=uuid4(),
        return_id=original.id,
        payment_id=original.payment_id,
        ledger_id=s["ledger"],
        group_version=2 if previous is None else previous[0]["group_version"] + 1,
        previous_revision_id=None if previous is None else previous[0]["id"],
        action="correct",
        revision_reason="Fictional check source prefix",
        return_date=date(2026, 10, 1),
        return_reason="Fictional new return facts",
        created_by=s["owner"],
        idempotency_key="fictional-prefix-" + str(uuid4()),
    )
    row = member(h, original) | dict(
        source_return_no=number,
        source_predecessor_operation_id=predecessor,
        prefix_payment_amount=Decimal(prefix[0]),
        prefix_document_amount=Decimal(prefix[1]),
        payment_amount=Decimal(payment),
        document_amount=Decimal(document),
        absorbs_remainder=absorbs,
    )
    if previous is not None:
        prior = previous[1]
        row.update(
            previous_operation_version=prior["operation_version"],
            operation_version=prior["operation_version"] + 1,
            previous_journal_id=prior["journal_id"],
        )
    row.update(changes or {})
    c.execute(insert(Revision).values(**h))
    c.execute(insert(Member).values(**row))
    return h, row


def test_saved_prefix_and_final_flag_survive_a_later_earlier_slot_change(drafts):
    s = drafts
    source, first, second, body = pair(s, "0.06", "0.03")
    before = snapshot(s)
    with s["engine"].connect() as c:
        for group in (first, second):
            prove(c, s["ledger"], group.id)
        later, _ = correction(
            c,
            s,
            second,
            payment="0.03",
            document="0.02",
            number=2,
            predecessor=first.postings[0].operation_id,
            prefix=("0.03", "0.02"),
            absorbs=True,
        )
        prove(c, s["ledger"], second.id, later["id"])
        revised, _ = correction(c, s, first, payment="0.02", document="0.01")
        validate(c, s["ledger"], [source])
        prove(c, s["ledger"], first.id, revised["id"])
        prove(c, s["ledger"], second.id, later["id"])
        prove(c, s["ledger"], first.id)
        prove(c, s["ledger"], second.id)
        c.rollback()
    assert submit_return(s, body) == first and snapshot(s) == before


@pytest.mark.parametrize(
    "fault", ["payment-prefix", "document-prefix", "false-final", "skipped-version"]
)
def test_plausible_effective_totals_do_not_prove_the_wrong_stored_prefix_or_version(drafts, fault):
    s = drafts
    source, first, second, _ = pair(s, "0.06", "0.03")
    changes = {
        "payment-prefix": {"prefix_payment_amount": Decimal("0.02")},
        "document-prefix": {"prefix_document_amount": Decimal("0.01")},
        "false-final": {"absorbs_remainder": False},
        "skipped-version": {"previous_operation_version": 2, "operation_version": 3},
    }[fault]
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        h, _ = correction(
            c,
            s,
            second,
            payment="0.03",
            document="0.02",
            number=2,
            predecessor=first.postings[0].operation_id,
            prefix=("0.03", "0.02"),
            absorbs=True,
            changes=changes,
        )
        validate(c, s["ledger"], [source])
        prove(c, s["ledger"], second.id, h["id"])
    assert caught.value.orig.diag.constraint_name == "ck_return_revision_source_history"
    assert snapshot(s) == before


def test_future_repair_cannot_hide_an_invalid_intermediate_rounding_state(drafts):
    s = drafts
    source, first, second, _ = pair(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        bad = correction(c, s, first, payment="0.01", document="0.01")
        repaired = correction(c, s, first, payment="0.03", document="0.02", previous=bad)
        validate(c, s["ledger"], [source])
        prove(c, s["ledger"], first.id, repaired[0]["id"])
        prove(c, s["ledger"], second.id)
        prove(c, s["ledger"], first.id, bad[0]["id"])
    assert caught.value.orig.diag.constraint_name == "ck_return_revision_source_history"
    assert snapshot(s) == before


@pytest.mark.parametrize("fault", ["ledger", "origin", "revision", "other-group"])
def test_history_lookup_fails_closed_outside_its_ledger_and_group(drafts, fault):
    s = drafts
    _, first, second, _ = pair(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        h, _ = correction(c, s, first, payment="0.03", document="0.02")
        ledger = uuid4() if fault == "ledger" else s["ledger"]
        group = uuid4() if fault == "origin" else second.id if fault == "other-group" else first.id
        rev = None if fault == "origin" else uuid4() if fault == "revision" else h["id"]
        prove(c, ledger, group, rev)
    assert caught.value.orig.diag.constraint_name == "ck_return_revision_source_history"
    assert snapshot(s) == before


def test_two_hundred_real_sources_retain_each_original_and_its_independent_revision_prefix(drafts):
    s = drafts
    _, _, _, body = setup_return(s, count=200)
    original = submit_return(s, body)
    before = snapshot(s)
    header = dict(
        id=uuid4(),
        return_id=original.id,
        payment_id=original.payment_id,
        ledger_id=s["ledger"],
        group_version=2,
        action="correct",
        revision_reason="Fictional bounded source proof",
        return_date=date(2026, 11, 8),
        return_reason="Fictional 200 independent prefixes",
        created_by=s["owner"],
        idempotency_key="fictional-bounded-prefix",
    )
    members = []
    for n, fact in enumerate(original.postings, 1):
        data = member(header, original) | dict(
            member_no=n,
            previous_line_no=n,
            line_no=n,
            source_allocation_line_id=fact.source_allocation_line_id,
            source_operation_id=fact.source_operation_id,
            operation_id=fact.operation_id,
            previous_journal_id=fact.journal_id,
            payment_amount=Decimal("0.01"),
            document_amount=Decimal("0.01"),
        )
        members.append(data)
    assert len(members) == 200
    with s["engine"].connect() as c:
        c.execute(insert(Revision).values(**header))
        c.execute(insert(Member), members)
        for version in (None, header["id"]):
            prove(c, s["ledger"], original.id, version)
        with pytest.raises(IntegrityError) as error:
            c.exec_driver_sql("SET CONSTRAINTS tr_business_return_revisions_complete IMMEDIATE")
        assert error.value.orig.diag.constraint_name == "ck_return_revision_shape"
        c.rollback()
    assert submit_return(s, body) == original and snapshot(s) == before
