"""Prove retained and retired positions against committed multi-asset source histories."""

from uuid import uuid4

import pytest
from coinpup_api.business.allocation_return_revision_slots import plan_source_slots
from coinpup_api.business.allocation_return_sources import original_sources, plan_sources
from coinpup_api.business.settlement_models import BusinessPayment
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import AssetRecord
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_return_journal_transitions import stage_transition
from tests.integration.business.test_return_source_sequences import pair
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def context(session, s, body):
    catalog = {r.asset_id: asset_definition(r) for r in session.scalars(select(AssetRecord))}
    payer = session.get(BusinessPayment, body.payment_id)
    _, sources, _ = plan_sources(
        session, s["ledger"], payer, original_sources(session, s["ledger"], body), catalog
    )
    return sources


@pytest.mark.parametrize(
    "source_payment,last_payment,can_remove", [("0.05", "0.02", False), ("0.06", "0.03", True)]
)
def test_locked_earlier_removal_proves_the_permanent_later_rounding(
    drafts, source_payment, last_payment, can_remove
):
    s = drafts
    key, first, last, body = pair(s, source_payment, last_payment)
    before = snapshot(s)
    with s["engine"].begin() as c, Session(bind=c) as session:
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        sources = context(session, s, body)
        statements = []

        def counted(conn, cursor, sql, parameters, ctx, many):
            statements.append(sql)

        event.listen(c, "before_cursor_execute", counted)
        if can_remove:
            removed = plan_source_slots(
                session, s["ledger"], body.payment_id, first.id, sources, []
            )[key]
            assert removed.predecessor is None and removed.replacement is None
            assert removed.sequence.source_return_no == 1 and removed.absorbs_remainder is None
            assert removed.sequence.total_document.minor_units == 2
        else:
            with pytest.raises(MoneyError) as exc:
                plan_source_slots(session, s["ledger"], body.payment_id, first.id, sources, [])
            assert exc.value.code == "allocation_return_ratio"
        assert len(statements) == 2 and all(q.startswith("SELECT") for q in statements)
        line = body.lines[0].model_copy(update={"payment_amount": last_payment})
        current = plan_source_slots(
            session, s["ledger"], body.payment_id, last.id, sources, [line]
        )[key]
        assert current.sequence.source_return_no == 2
        assert current.predecessor == first.postings[0].operation_id
        assert (
            current.sequence.previous_payment.minor_units == 3
            and current.sequence.previous_document.minor_units == 2
        )
        assert current.absorbs_remainder is True
        for ledger, payment in [(uuid4(), body.payment_id), (s["ledger"], uuid4())]:
            with pytest.raises(LedgerError) as invalid:
                plan_source_slots(session, ledger, payment, first.id, sources, [])
            assert (invalid.value.status, invalid.value.code) == (
                503,
                "allocation_return_integrity",
            )
    assert snapshot(s) == before


def test_rejoin_after_complete_retirement_uses_new_birth_but_keeps_every_old_slot(drafts):
    s = drafts
    key, first, last, body = pair(s, "0.06", "0.03")
    with s["engine"].begin() as c:
        stage_transition(c, (s, body, first), asset="EUR", cancel=True)
    before = snapshot(s)
    with s["engine"].begin() as c, Session(bind=c) as session:
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        sources = context(session, s, body)
        planned = plan_source_slots(
            session, s["ledger"], body.payment_id, uuid4(), sources, body.lines
        )[key]
        assert planned.sequence.source_return_no == 3
        assert planned.predecessor == last.postings[0].operation_id
        assert planned.sequence.previous_payment.minor_units == 3
        assert planned.sequence.previous_document.minor_units == 2 and planned.absorbs_remainder
    assert snapshot(s) == before
