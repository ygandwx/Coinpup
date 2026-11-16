"""Prove real historical refund journals using the dates of the return versions actually bound."""

from datetime import date
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.refund_models import BusinessRefund, BusinessRefundReturn
from coinpup_api.business.refund_revision_models import BusinessRefundRevision as RefundRevision
from coinpup_api.business.refund_revision_return_models import (
    BusinessRefundRevisionReturn as Evidence,
)
from coinpup_api.business.refund_schemas import BusinessRefundCreate
from coinpup_api.ledger.models import CommandReceipt, FinancialOperation
from coinpup_api.sync.locking import acquire_write_lock
from sqlalchemy import insert, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_credit_fact_validation import copy_posting, stored
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_bound_return_versions import revise_return
from tests.integration.business.test_refund_historical_validation import check_origin, check_posting
from tests.integration.business.test_refund_revision_headers import replacement
from tests.integration.business.test_refund_service import refund
from tests.integration.conftest import refund_bound_date_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def original(s):
    payment, _, _, command = setup_return(s)
    command = command.model_copy(update={"return_date": date(2026, 10, 3)})
    returned = submit_return(s, command)
    body = BusinessRefundCreate(
        id=uuid4(),
        payment_id=payment.id,
        expected_version=1,
        account_id=payment.account_id,
        asset_id="USD",
        amount="5",
        payment_amount="5",
        transaction_date="2026-10-04",
        reason="Fictional refund with bound date",
        allocation_return_ids=[returned.id],
    )
    return returned, body, refund(s, body)


def staged(c, s, ret, body, old, kind, day):
    acquire_write_lock(c)
    new_journal = uuid4()
    note = "Fictional second cash movement" if kind == "origin" else "Fictional changed cash record"
    journal = dict(id=new_journal, transaction_date=day, recognition_date=day, description=note)
    key = "fictional-refund-pinned-proof"
    reference = dict(
        id=uuid4(),
        return_id=ret.id,
        return_version=2,
        ledger_id=s["ledger"],
        payment_id=old.payment_id,
        line_no=1,
    )
    if kind == "origin":
        new_id, op = uuid4(), uuid4()
        c.execute(
            insert(BusinessRefund).values(
                **(
                    stored(c, BusinessRefund, old.id)
                    | dict(
                        id=new_id,
                        operation_id=op,
                        journal_id=new_journal,
                        transaction_date=day,
                        reason=note,
                        idempotency_key=key,
                    )
                )
            )
        )
        c.execute(
            insert(FinancialOperation).values(
                **(
                    stored(c, FinancialOperation, old.operation_id)
                    | dict(id=op, current_journal_id=new_journal)
                )
            )
        )
        c.execute(insert(BusinessRefundReturn).values(**reference, refund_id=new_id))
        copy_posting(c, old.journal_id, journal | dict(operation_id=op))
        fixed = c.scalar(text("SELECT coinpup_business_refund_response(:id)"), {"id": new_id})
        c.execute(
            insert(CommandReceipt).values(
                ledger_id=s["ledger"],
                key=key,
                hash_version=2,
                request_hash="a" * 64,
                operation_id=op,
                response_status=201,
                response=fixed,
            )
        )
    else:
        new_id = uuid4()
        data = replacement(body) | dict(
            id=new_id,
            ledger_id=s["ledger"],
            refund_id=old.id,
            payment_id=old.payment_id,
            operation_id=old.operation_id,
            operation_version=2,
            previous_journal_id=old.journal_id,
            reversal_journal_id=uuid4(),
            replacement_journal_id=new_journal,
            idempotency_key=key,
            revision_reason="Fictional historical correction",
            created_by=s["owner"],
            amount=old.amount,
            payment_amount=old.payment_amount,
            reason=note,
            transaction_date=day,
        )
        c.execute(insert(RefundRevision).values(**data))
        c.execute(insert(Evidence).values(**reference, refund_id=old.id, revision_id=new_id))
        c.execute(
            update(FinancialOperation)
            .where(FinancialOperation.id == old.operation_id)
            .values(version=2, current_journal_id=new_journal)
        )
        copy_posting(
            c,
            old.journal_id,
            journal | dict(operation_version=2, revision_reason=data["revision_reason"]),
        )
    return new_id, new_journal


@pytest.mark.parametrize("kind", ["origin", "replacement"])
@pytest.mark.parametrize("valid", [True, False])
def test_posting_proof_uses_bound_date_even_when_v1_or_the_later_current_date_would_disagree(
    drafts, kind, valid
):
    s = drafts
    ret, body, old = original(s)
    before = snapshot(s)
    day = date(2026, 10, 2) if valid else date(2026, 10, 4)
    pinned = date(2026, 10, 1) if valid else date(2026, 10, 5)
    future = date(2026, 10, 5) if valid else date(2026, 10, 1)
    with s["engine"].connect() as c:
        v2 = revise_return(c, s, ret, pinned)
        identifier, journal = staged(c, s, ret, body, old, kind, day)
        revise_return(c, s, ret, future, previous=v2)
        check_origin(c, old.id)
        check_posting(c, old.journal_id)
        if valid:
            check_posting(c, journal)
            if kind == "origin":
                check_origin(c, identifier)
        else:
            with pytest.raises(IntegrityError) as rejected:
                check_posting(c, journal)
            assert rejected.value.orig.diag.constraint_name == "ck_business_refund_shape"
        c.rollback()
    assert refund(s, body) == old and snapshot(s) == before


def test_historical_matching_requires_the_ordered_evidence_and_exact_group_scope(drafts):
    s = drafts
    ret, body, old = original(s)
    before = snapshot(s)
    params = dict(
        l=s["ledger"], p=old.payment_id, f=old.id, r=None, ids=[ret.id], day=old.transaction_date
    )
    query = text("SELECT coinpup_refund_evidence_at_date(:l,:p,:f,:r,CAST(:ids AS uuid[]),:day)")
    with s["engine"].connect() as c:
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        assert c.scalar(query, params) is True
        for change in (
            {"ids": []},
            {"ids": None},
            {"ids": [ret.id, ret.id]},
            {"l": uuid4()},
            {"p": uuid4()},
            {"r": uuid4()},
            {"f": uuid4()},
            {"day": date(2026, 10, 2)},
        ):
            assert c.scalar(query, params | change) is False
    assert refund(s, body) == old and snapshot(s) == before


def test_empty_bound_date_migration_roundtrip_restores_exact_prior_financial_sql(drafts):
    s = drafts
    ret, body, old = original(s)
    before = snapshot(s)
    m = refund_bound_date_migration()
    lookup = text(
        "SELECT pg_get_functiondef("
        "'public.coinpup_validate_refund_posting_fact(uuid,jsonb)'::regprocedure)"
    )
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        live = definitions(c)
        assert c.scalar(lookup) == m["_POSTING"]
        m["downgrade"]()
        assert c.scalar(lookup) == m["_OLD_POSTING"]
        check_origin(c, old.id)
        m["upgrade"]()
        assert definitions(c) == live
    with pytest.raises(IntegrityError) as err, s["engine"].begin() as c:
        revise_return(c, s, ret)
        with Operations.context(MigrationContext.configure(c)):
            m["downgrade"]()
    assert err.value.orig.diag.constraint_name == "ck_return_revision_downgrade"
    assert refund(s, body) == old and snapshot(s) == before
