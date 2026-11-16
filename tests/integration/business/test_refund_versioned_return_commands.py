"""Exercise refund commands against versioned evidence in a disposable transaction."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from coinpup_api.business.allocation_return_revision_member_models import (
    BusinessReturnRevisionMember as Member,
)
from coinpup_api.business.refund_evidence import bound_return_sources
from coinpup_api.business.refund_models import BusinessRefundReturn
from coinpup_api.business.refund_revision_return_models import BusinessRefundRevisionReturn
from coinpup_api.business.refund_revision_schemas import RefundCorrectionCreate
from coinpup_api.business.refund_revisions import BusinessRefundRevisionService
from coinpup_api.business.refunds import BusinessRefundService
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event, insert, select
from sqlalchemy.orm import Session

from tests.integration.business.test_allocation_return_revision_members import member
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_bound_evidence_dates import original
from tests.integration.business.test_refund_bound_return_versions import revise_return
from tests.integration.business.test_refund_historical_validation import check_origin, check_posting
from tests.integration.business.test_refund_revision_service import correction
from tests.integration.business.test_refund_service import refund
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def replace_return_date(c, s, ret, on):
    h = revise_return(c, s, ret, on)
    first = ret.postings[0]
    details = member(h, ret)
    if on is None:
        details.update(
            action="cancel",
            line_no=None,
            journal_id=None,
            payment_amount=None,
            document_amount=None,
            absorbs_remainder=None,
        )
    else:
        details.update(
            payment_amount=Decimal(first.payment_amount),
            document_amount=Decimal(first.document_amount),
        )
    c.execute(insert(Member).values(**details))
    return h


def command(s, connection, kind, body, old, *, day, identifiers, key):
    if kind == "create":
        payload = body.model_copy(
            update={"id": uuid4(), "transaction_date": day, "allocation_return_ids": identifiers}
        )
        service = BusinessRefundService(connection)
        return lambda: service.create(s["owner"], s["ledger"], payload, key)
    values = correction(
        s,
        old,
        1,
        amount="5",
        payment_amount="5",
        transaction_date=day.isoformat(),
        allocation_return_ids=[str(x) for x in identifiers],
    )
    payload = RefundCorrectionCreate.model_validate(values)
    service = BusinessRefundRevisionService(connection)
    return lambda: service.correct(s["owner"], s["ledger"], old.id, payload, key)


@pytest.mark.parametrize("kind", ["create", "correct"])
def test_normal_commands_use_earlier_effective_date_and_replay_the_same_bound_facts(drafts, kind):
    s = drafts
    ret, body, old = original(s)
    before = snapshot(s)
    with s["engine"].connect() as c:
        c.begin()
        v2 = replace_return_date(c, s, ret, date(2026, 10, 1))
        submit = command(
            s,
            c,
            kind,
            body,
            old,
            day=date(2026, 10, 2),
            identifiers=[ret.id],
            key="fictional-new-pinned",
        )
        result = submit()
        if kind == "create":
            statement = select(
                BusinessRefundReturn.return_id, BusinessRefundReturn.return_version
            ).where(BusinessRefundReturn.refund_id == result.id)
            check_origin(c, result.id)
        else:
            statement = select(
                BusinessRefundRevisionReturn.return_id, BusinessRefundRevisionReturn.return_version
            ).where(BusinessRefundRevisionReturn.revision_id == result.revision_id)
            check_posting(c, result.replacement_journal_id)
        assert c.execute(statement).all() == [(ret.id, 2)]
        revise_return(c, s, ret, None, v2)
        assert submit() == result
        assert c.execute(statement).all() == [(ret.id, 2)]
        c.rollback()
    assert refund(s, body) == old and snapshot(s) == before


@pytest.mark.parametrize("kind", ["create", "correct"])
@pytest.mark.parametrize(
    "fault,code,status",
    [
        ("later", "refund_date", 422),
        ("cancelled", "operation_cancelled", 409),
        ("missing", "not_found", 404),
        ("mixed-scope", "not_found", 404),
    ],
)
def test_commands_reject_the_current_date_terminal_group_and_out_of_scope_references(
    drafts, kind, fault, code, status
):
    s = drafts
    ret, body, old = original(s)
    before = snapshot(s)
    with s["engine"].connect() as c:
        c.begin()
        replace_return_date(
            c, s, ret, None if fault in {"cancelled", "mixed-scope"} else date(2026, 10, 5)
        )
        links = {"missing": [uuid4()], "mixed-scope": [ret.id, uuid4()]}.get(fault, [ret.id])
        submit = command(
            s,
            c,
            kind,
            body,
            old,
            day=date(2026, 10, 4),
            identifiers=links,
            key="fictional-reject-binding",
        )
        with pytest.raises(LedgerError) as rejected:
            submit()
        assert (rejected.value.code, rejected.value.status) == (code, status)
        c.rollback()
    assert refund(s, body) == old and snapshot(s) == before


def test_evidence_reader_batches_candidates_and_skips_empty_sets(drafts):
    s = drafts
    ret, _, _ = original(s)
    statements = []

    def seen(conn, cursor, sql, parameters, context, many):
        if "coinpup_current_return_evidence" in sql:
            statements.append(sql)

    with Session(s["engine"]) as session:
        c = session.connection()
        event.listen(c, "before_cursor_execute", seen)
        try:
            assert bound_return_sources(session, s["ledger"], ret.payment_id, []) == {}
            assert statements == []
            found = bound_return_sources(session, s["ledger"], ret.payment_id, [ret.id])
            assert (found[ret.id].version, found[ret.id].return_date) == (1, ret.return_date)
            assert len(statements) == 1
        finally:
            event.remove(c, "before_cursor_execute", seen)
