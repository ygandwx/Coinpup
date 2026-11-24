"""Exercise the real reservation command with the unmodified closed commit gate."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from coinpup_api.business.invoice_completion_models import InvoiceCompletion
from coinpup_api.business.invoice_issuance_schemas import InvoiceIssuanceCreate
from coinpup_api.business.invoice_profile_models import InvoiceProfile
from coinpup_api.business.invoice_reservation_command import (
    InvoiceReservationCommand,
    _reserve_or_read,
)
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.ledger.models import Entity
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.business.test_invoice_command_key import financial
from tests.integration.business.test_invoice_completions import completion
from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_snapshot_binding import company as company
from tests.integration.business.test_invoice_snapshot_binding import reviewed
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def args(s, record, *, body=None, key=None):
    raw = record["request_json"].encode() if body is None else json.dumps(body).encode()
    return dict(
        owner=s["owner"],
        ledger=s["ledger"],
        command=InvoiceIssuanceCreate.model_validate_json(raw),
        key=key or record["idempotency_key"],
        raw_body=raw,
    )


@pytest.mark.parametrize("completed", [False, True])
def test_original_intent_replays_before_current_mutability_checks_and_writes_nothing(
    company, completed
):
    s = company
    record = reviewed(s, logo=True, uppercase=True)
    done = completion(s, record) if completed else None
    original = snapshot(s)
    service = InvoiceReservationCommand(s["engine"])
    with s["engine"].connect() as connection, connection.begin() as transaction:
        connection.execute(insert(InvoiceReservation).values(**record))
        if done:
            connection.execute(insert(InvoiceCompletion).values(**done))
        connection.execute(
            update(InvoiceProfile)
            .where(InvoiceProfile.id == record["configuration_id"])
            .values(archived=True, version=2)
        )
        with Session(bind=connection, join_transaction_mode="rollback_only") as session:
            _, entity = service._locked_ledger(session, s["owner"], s["ledger"])
            session.execute(
                update(Entity).where(Entity.id == entity.id).values(archived=True, version=2)
            )
            before = session.scalar(select(func.count()).select_from(InvoiceReservation))
            assert entity.archived
            for _ in range(2):
                result = _reserve_or_read(session, entity=entity, **args(s, record))
                assert result.state == ("complete" if done else "pending")
                if done:
                    assert result.model_dump(mode="json") == done["response"]
                else:
                    assert result.artifacts == () and result.number == record["number"]
            assert (
                before == session.scalar(select(func.count()).select_from(InvoiceReservation)) == 1
            )
            assert not session.new and not session.dirty
        transaction.rollback()
    assert snapshot(s) == original


@pytest.mark.parametrize("change", ["new_key", "new_id", "raw_spelling", "other_document"])
def test_same_intent_or_scoped_key_cannot_be_rebound(company, change):
    s = company
    record = reviewed(s, uppercase=True)
    original = snapshot(s)
    service = InvoiceReservationCommand(s["engine"])
    with s["engine"].connect() as c, c.begin() as tx:
        c.execute(insert(InvoiceReservation).values(**record))
        with Session(bind=c, join_transaction_mode="rollback_only") as session:
            _, entity = service._locked_ledger(session, s["owner"], s["ledger"])
            raw = json.loads(record["request_json"])
            if change == "new_id":
                raw["id"] = str(uuid4())
            elif change == "other_document":
                raw["document_id"] = str(uuid4())
            elif change == "raw_spelling":
                raw["id"] = raw["id"].lower()
            with pytest.raises(LedgerError) as mismatch:
                _reserve_or_read(
                    session,
                    entity=entity,
                    **args(
                        s,
                        record,
                        body=raw,
                        key="fictional-new-key" if change == "new_key" else None,
                    ),
                )
            assert (mismatch.value.status, mismatch.value.code) == (409, "idempotency_conflict")
        tx.rollback()
    assert snapshot(s) == original


def test_financial_receipt_wins_and_remains_exactly_replayable(company):
    s = company
    record = reviewed(s)
    _, post = financial(s, record["idempotency_key"])
    receipt = post()
    original = snapshot(s)
    with pytest.raises(LedgerError) as taken:
        InvoiceReservationCommand(s["engine"]).reserve(**args(s, record))
    assert taken.value.code == "idempotency_conflict" and post() == receipt
    assert snapshot(s) == original


@pytest.mark.parametrize(
    "fault,code",
    [
        ("owner", "not_found"),
        ("ledger", "not_found"),
        ("personal", "invoice_requires_company"),
        ("archived", "entity_archived"),
        ("key", "invalid_idempotency_key"),
    ],
)
def test_scope_eligibility_and_key_rejected_before_any_write(company, fault, code):
    s = company
    record = reviewed(s)
    kw = args(s, record)
    if fault == "owner":
        kw["owner"] = uuid4()
    elif fault == "ledger":
        kw["ledger"] = uuid4()
    elif fault == "personal":
        kw["ledger"] = s["entity"].ledger.id
    elif fault == "key":
        kw["key"] = " invalid"
    else:
        with s["engine"].begin() as c:
            c.execute(
                update(Entity).where(Entity.kind == "company").values(archived=True, version=2)
            )
    original = snapshot(s)
    with pytest.raises(LedgerError) as rejected:
        InvoiceReservationCommand(s["engine"]).reserve(**kw)
    assert rejected.value.code == code and snapshot(s) == original


def test_two_equal_first_attempts_cannot_return_success_or_leave_rows_when_commit_gate_is_closed(
    company,
):
    s = company
    record = reviewed(s)
    kw = args(s, record)
    before = snapshot(s)
    barrier = Barrier(2)
    service = InvoiceReservationCommand(s["engine"])

    def run():
        barrier.wait(10)
        with pytest.raises(LedgerError) as stopped:
            service.reserve(**kw)
        assert stopped.value.code == "constraint_conflict"
        cause = stopped.value.__context__
        assert isinstance(cause, IntegrityError)
        assert cause.orig.diag.constraint_name == "ck_invoice_reservation_complete"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        for future in futures:
            future.result(timeout=30)
    assert snapshot(s) == before
