"""The adapter checks real frozen SQL facts and a ledger-scoped original key before replay."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from coinpup_api.business.refund_revision_hash import refund_revision_hash_v2
from coinpup_api.business.refund_revision_receipts import replay_revision, response_from_rows
from coinpup_api.business.refund_revision_schemas import RefundRevisionResponse
from coinpup_api.ledger.models import CommandReceipt
from coinpup_api.ledger.service import LedgerError
from sqlalchemy.orm import Session

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_evidence import evidence as evidence
from tests.integration.business.test_refund_revision_receipt import (
    add_receipt,
    correct,
    response,
    terminal,
)
from tests.integration.business.test_refund_service import refund
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.unit.business.test_refund_revision_replay import intent

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("action", ["correct", "cancel"])
def test_replay_reads_the_named_version_after_a_later_cancellation_and_preserves_the_201(
    evidence, action
):
    s, _, body, original, _ = evidence
    before = snapshot(s)
    with s["engine"].connect() as c, Session(bind=c) as session:
        first = correct(c, evidence, keep_evidence=True)
        last = terminal(c, evidence, first)
        for row in (first, last):
            fixed = RefundRevisionResponse.model_validate(response(c, row["id"]))
            command = intent(fixed)
            digest = refund_revision_hash_v2(row["action"], s["ledger"], original.id, command)
            add_receipt(c, row, fixed.model_dump(mode="json"), request_hash=digest)
        row = first if action == "correct" else last
        fixed = response_from_rows(session, row["id"])
        command = intent(fixed)
        digest = refund_revision_hash_v2(action, s["ledger"], original.id, command)
        receipt = session.get(CommandReceipt, (s["ledger"], row["idempotency_key"]))
        assert receipt.request_hash == digest
        for _ in range(2):
            assert (
                replay_revision(session, receipt, s["ledger"], original.id, action, command, digest)
                == fixed
            )
        changed = command.model_copy(update={"reason": "Fictional different intent"})
        changed_hash = refund_revision_hash_v2(action, s["ledger"], original.id, changed)
        with pytest.raises(LedgerError) as conflict:
            replay_revision(
                session, receipt, s["ledger"], original.id, action, changed, changed_hash
            )
        assert (conflict.value.status, conflict.value.code) == (409, "idempotency_conflict")
    assert refund(s, body) == original and snapshot(s) == before


@pytest.mark.parametrize(
    "bad",
    [
        "scope",
        "route",
        "key",
        "payment",
        "operation",
        "status",
        "version",
        "source",
        "fees",
        "extra",
        "amount",
    ],
)
def test_replay_rejects_valid_looking_partial_or_wrongly_scoped_receipts(evidence, bad):
    s, _, body, original, _ = evidence
    before = snapshot(s)
    with s["engine"].connect() as c, Session(bind=c) as session:
        row = correct(c, evidence, keep_evidence=True)
        expected = response_from_rows(session, row["id"])
        command = intent(expected)
        digest = refund_revision_hash_v2("correct", s["ledger"], original.id, command)
        supplied = deepcopy(expected.model_dump(mode="json"))
        receipt = SimpleNamespace(
            ledger_id=s["ledger"],
            key=row["idempotency_key"],
            hash_version=2,
            request_hash=digest,
            response_status=200,
            operation_id=original.operation_id,
            response=supplied,
        )
        target = original.id
        if bad == "scope":
            receipt.ledger_id = uuid4()
        if bad == "route":
            target = uuid4()
        if bad == "key":
            receipt.key = "fictional-other-key"
        if bad == "payment":
            supplied["payment_id"] = str(uuid4())
        if bad == "operation":
            receipt.operation_id = uuid4()
        if bad == "status":
            receipt.response_status = 201
        if bad == "version":
            supplied["version"] = float(supplied["version"])
        if bad == "source":
            supplied["previous"]["source_version"] = True
        if bad == "fees":
            supplied["replacement"]["fees"].reverse()
        if bad == "extra":
            supplied["unused"] = None
        if bad == "amount":
            supplied["replacement"]["amount"] = "18.73"
        with pytest.raises(LedgerError) as rejected:
            replay_revision(session, receipt, s["ledger"], target, "correct", command, digest)
        assert (rejected.value.status, rejected.value.code) == (
            503,
            "refund_revision_integrity",
        )
    assert refund(s, body) == original and snapshot(s) == before
