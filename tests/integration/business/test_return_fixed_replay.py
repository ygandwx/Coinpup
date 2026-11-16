"""Re-read committed return corrections and cancellations in an explicitly read-only snapshot."""

import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from coinpup_api.business.allocation_return_revision_hash import allocation_return_revision_hash_v2
from coinpup_api.business.allocation_return_revision_receipts import (
    replay_revision,
    response_from_rows,
)
from coinpup_api.business.allocation_return_revision_schemas import (
    AllocationReturnCancellationCreate,
    AllocationReturnCorrectionCreate,
)
from coinpup_api.ledger.models import CommandReceipt
from coinpup_api.ledger.schemas import AssetUpdate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy.orm import Session

from tests.integration.business.test_allocation_return_service import submit_return
from tests.integration.business.test_credit_revision_receipts import content
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_return_complete_lifecycle import fixed
from tests.integration.business.test_return_journal_transitions import group as group
from tests.integration.business.test_return_journal_transitions import stage_transition
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def submit_fixture(setup, asset, *, prior=None):
    s, original_body, original = setup
    if prior is None:
        action = "correct"
        raw = dict(
            expected_version=1,
            reason="  Fictional correction  ",
            replacement=dict(
                return_date="2026-11-08",
                reason="Fictional new group 中文",
                lines=[line.model_dump(mode="json") for line in reversed(original_body.lines)],
            ),
        )
        command = AllocationReturnCorrectionCreate.model_validate(raw)
    else:
        action = "cancel"
        raw = dict(expected_version=prior["version"], reason="  Fictional cancel  ")
        command = AllocationReturnCancellationCreate.model_validate(raw)
    digest = allocation_return_revision_hash_v2(
        action,
        s["ledger"],
        original.id,
        command,
        raw_body=json.dumps(raw, ensure_ascii=False).encode(),
    )
    with s["engine"].begin() as c:
        h = stage_transition(
            c, setup, asset=asset, prior=prior, cancel=prior is not None, request_hash=digest
        )
        expected = fixed(c, s["ledger"], h["idempotency_key"])
    return h["idempotency_key"], command, digest, expected


@pytest.mark.parametrize("group", [("USD", "receipt"), ("EUR", "payout")], indirect=True)
def test_fixed_correct_and_cancel_after_closure_asset_disable_and_later_version(group):
    setup, asset = group
    s, body, original = setup
    first = submit_fixture(setup, asset)
    last = submit_fixture(setup, asset, prior=first[-1])
    change_period(s, cutoff="2026-11-08")
    for identity in sorted({asset, "USD"}):
        s["structure"].update_asset(
            s["owner"], identity, AssetUpdate(expected_version=1, enabled=False)
        )
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    with s["engine"].begin() as c, Session(bind=c) as session:
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        frozen = content(c)
        for key, command, digest, expected in (last, first, last):
            receipt = session.get(CommandReceipt, (s["ledger"], key))
            actual = replay_revision(
                session, receipt, s["ledger"], original.id, expected["action"], command, digest
            )
            assert actual.model_dump(mode="json") == expected
            assert response_from_rows(session, actual.revision_id) == actual
            normalized = allocation_return_revision_hash_v2(
                expected["action"], s["ledger"], original.id, command
            )
            with pytest.raises(LedgerError) as conflict:
                replay_revision(
                    session,
                    receipt,
                    s["ledger"],
                    original.id,
                    expected["action"],
                    command,
                    normalized,
                )
            assert (conflict.value.status, conflict.value.code) == (409, "idempotency_conflict")
        assert content(c) == frozen
    assert submit_return(s, body) == original and snapshot(s) == before


def test_inconsistent_receipt_scope_and_ordered_command_are_never_accepted(group):
    setup, asset = group
    s, _, original = setup
    key, command, digest, expected = submit_fixture(setup, asset)
    before = snapshot(s)
    with s["engine"].begin() as c, Session(bind=c) as session:
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        frozen = content(c)
        source = session.get(CommandReceipt, (s["ledger"], key))
        fields = {
            k: getattr(source, k)
            for k in (
                "ledger_id",
                "key",
                "hash_version",
                "request_hash",
                "response_status",
                "operation_id",
                "response",
            )
        }

        def reject(
            receipt=source,
            *,
            scope=s["ledger"],
            target=original.id,
            payload=command,
            action="correct",
        ):
            with pytest.raises(LedgerError) as err:
                replay_revision(session, receipt, scope, target, action, payload, digest)
            assert (err.value.status, err.value.code) == (503, "allocation_return_integrity")

        reject(None)
        reject(scope=uuid4())
        reject(target=uuid4())
        reject(action="cancel")
        for changes in (
            dict(operation_id=uuid4()),
            dict(response_status=201),
            dict(key="another-key"),
        ):
            reject(SimpleNamespace(**(fields | changes)))
        for field, value in [
            ("payment_id", str(uuid4())),
            ("version", 2.0),
            ("revision_id", expected["revision_id"].replace("-", "")),
        ]:
            reject(SimpleNamespace(**(fields | dict(response=expected | {field: value}))))
        changed = deepcopy(expected)
        changed["members"][0]["replacement"]["document_amount"] = "10"
        reject(SimpleNamespace(**(fields | dict(response=changed))))
        for field, value in [("payment_amount", "9.99"), ("document_amount", "10.01")]:
            replacement = command.replacement.model_copy(
                update={
                    "lines": [
                        command.replacement.lines[0].model_copy(update={field: value}),
                        command.replacement.lines[1],
                    ]
                }
            )
            reject(payload=command.model_copy(update={"replacement": replacement}))
        replacement = command.replacement.model_copy(
            update={"lines": list(reversed(command.replacement.lines))}
        )
        reject(payload=command.model_copy(update={"replacement": replacement}))
        with pytest.raises(LedgerError) as no_rows, session.begin_nested():
            response_from_rows(session, uuid4())
        assert (no_rows.value.status, no_rows.value.code) == (503, "allocation_return_integrity")
        assert content(c) == frozen
    assert snapshot(s) == before
