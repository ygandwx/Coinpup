"""Historical return postings survive later corrections and cancellations."""

from decimal import Decimal
from uuid import uuid4

import pytest
from coinpup_api.business.allocation_return_posting_facts import (
    return_posting_fact,
    return_posting_facts,
)
from coinpup_api.business.allocation_return_revision_member_models import (
    BusinessReturnRevisionMember as Member,
)
from coinpup_api.business.allocation_return_revision_models import (
    BusinessAllocationReturnRevision as Revision,
)
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import insert, select

from tests.integration.business.test_allocation_return_revision_headers import revision as revision
from tests.integration.business.test_allocation_return_revision_members import member
from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("direction,asset", [("receipt", "USD"), ("payout", "EUR")])
def test_original_postings_preserve_dimensions_and_separate_asset_identities(
    drafts, direction, asset
):
    s = drafts
    _, _, _, request = setup_return(s, direction=direction, asset=asset, count=2)
    original = submit_return(s, request)
    before = snapshot(s)
    with s["engine"].connect() as c:
        facts = return_posting_facts(s["ledger"], return_ids=[original.id])
        rows = c.execute(select(facts).order_by(facts.c.line_no)).mappings().all()
        assert len(rows) == 2
        for row, posting in zip(rows, original.postings, strict=True):
            assert row == return_posting_fact(c, s["ledger"], posting.journal_id)
            assert (
                row["return_id"],
                row["ledger_id"],
                row["payment_id"],
                row["group_version"],
                row["revision_id"],
                row["created_by"],
                row["return_date"],
                row["return_reason"],
                row["revision_reason"],
            ) == (
                original.id,
                s["ledger"],
                original.payment_id,
                1,
                None,
                s["owner"],
                original.return_date,
                request.reason,
                None,
            )
            assert (
                row["direction"],
                row["party_id"],
                row["document_id"],
                row["document_line_id"],
                row["payment_asset_id"],
                row["document_asset_id"],
                row["source_operation_id"],
                row["source_version"],
                row["source_return_no"],
                row["operation_id"],
                row["operation_version"],
            ) == (
                direction,
                s["party"].id,
                posting.document_id,
                posting.document_line_id,
                "USD",
                asset,
                posting.source_operation_id,
                1,
                1,
                posting.operation_id,
                1,
            )
            assert row["payment_amount"] == Decimal(posting.payment_amount) and row[
                "document_amount"
            ] == Decimal(posting.document_amount)
        for requested in (
            {},
            {"return_ids": []},
            {"journal_ids": []},
            {"return_ids": [uuid4()], "journal_ids": [original.postings[0].journal_id]},
        ):
            assert c.execute(select(return_posting_facts(s["ledger"], **requested))).all() == []
        with pytest.raises(LedgerError) as err:
            return_posting_fact(c, uuid4(), original.postings[0].journal_id)
        assert err.value.code == "allocation_return_integrity"
    assert submit_return(s, request) == original and snapshot(s) == before


def test_old_and_new_journals_keep_separate_amounts_dates_reasons_and_reference_after_cancel(
    revision,
):
    s, h, _, original = revision
    old = original.postings[0]
    m = member(h, original)
    before = snapshot(s)
    with s["engine"].connect() as c:
        first = return_posting_fact(c, s["ledger"], old.journal_id)
        c.execute(insert(Revision).values(**h))
        c.execute(insert(Member).values(**m))
        terminal = h | dict(
            id=uuid4(),
            group_version=3,
            previous_revision_id=h["id"],
            action="cancel",
            return_date=None,
            return_reason=None,
            idempotency_key="fictional-staged-cancel",
        )
        last = m | dict(
            id=uuid4(),
            revision_id=terminal["id"],
            action="cancel",
            previous_operation_version=2,
            operation_version=3,
            previous_journal_id=m["journal_id"],
            reversal_journal_id=uuid4(),
            line_no=None,
            journal_id=None,
            payment_amount=None,
            document_amount=None,
            absorbs_remainder=None,
        )
        c.execute(insert(Revision).values(**terminal))
        c.execute(insert(Member).values(**last))
        later = return_posting_fact(c, s["ledger"], m["journal_id"])
        assert return_posting_fact(c, s["ledger"], old.journal_id) == first
        assert (
            later["group_version"],
            later["operation_version"],
            later["revision_id"],
            later["operation_id"],
            later["return_date"],
            later["return_reason"],
            later["revision_reason"],
            later["source_return_no"],
            later["payment_amount"],
            later["document_amount"],
        ) == (
            2,
            2,
            h["id"],
            old.operation_id,
            h["return_date"],
            h["return_reason"],
            h["revision_reason"],
            1,
            Decimal("12.34"),
            Decimal("12.34"),
        )
        facts = return_posting_facts(s["ledger"], return_ids=[original.id])
        assert set(c.scalars(select(facts.c.journal_id))) == {old.journal_id, m["journal_id"]}
        for reversal in (last["reversal_journal_id"], m["reversal_journal_id"]):
            with pytest.raises(LedgerError):
                return_posting_fact(c, s["ledger"], reversal)
        c.rollback()
    assert snapshot(s) == before


def test_ambiguous_origin_or_replacement_journal_never_silently_selects_one(revision):
    s, h, _, original = revision
    old = original.postings[0]
    # The incomplete financial closure cannot commit. Test the historical reader's own guarantee.
    staged = member(h, original) | dict(journal_id=old.journal_id, previous_journal_id=uuid4())
    with s["engine"].connect() as c:
        c.execute(insert(Revision).values(**h))
        c.execute(insert(Member).values(**staged))
        with pytest.raises(LedgerError) as err:
            return_posting_fact(c, s["ledger"], old.journal_id)
        assert err.value.code == "allocation_return_integrity" and err.value.status == 503
        c.rollback()
