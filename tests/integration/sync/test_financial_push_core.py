"""Real ledger reuse, atomic enclosing writes and immutable two-version evidence."""

import json
from uuid import uuid4

import pytest
from coinpup_api.ledger.service import LedgerError
from coinpup_api.sync.financial_commands import apply_financial_push
from coinpup_api.sync.financial_conflicts import competing_versions
from coinpup_api.sync.financial_push import freeze_financial_push

from tests.integration.ledger.test_posting_service_database import (
    classified,
    financial_counts,
    opening,
    quantities,
)
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def _prepared(s):
    s["posting"].post_opening(s["owner"], s["ledger"], opening(s, "1000.00"), "fictional-opening")
    return classified(s, amount="100.00", id=uuid4()).model_dump(mode="json", exclude_unset=True)


def _revision(s, kind, operation, payload):
    return freeze_financial_push(
        s["ledger"], {"kind": kind, "operation_id": operation, "payload": payload}
    )


def _correction(payload, amount, expected=1):
    replacement = {key: value for key, value in payload.items() if key != "id"}
    replacement.update(
        kind="expense", amount=amount, splits=[payload["splits"][0] | {"amount": amount}]
    )
    return {
        "expected_version": expected,
        "reason": "Fictional device correction",
        "replacement": replacement,
    }


def test_sync_replay_uses_the_original_http_receipt_without_posting_again(
    ledger_setup, authenticated_client
):
    s = ledger_setup
    client, _, _ = authenticated_client
    payload = _prepared(s)
    request = {"kind": "expense", "payload": payload}
    push = freeze_financial_push(s["ledger"], request)
    path = f"/api/v1/ledgers/{s['ledger']}/expenses"
    headers = {"Idempotency-Key": "fictional-same-command"}
    posted = client.post(path, json=payload, headers=headers)
    assert posted.status_code == 201, posted.text
    before = financial_counts(s["engine"])
    with s["structure"]._transaction(s["owner"], write=True, read_committed=True) as session:
        replay = apply_financial_push(session, s["owner"], push, headers["Idempotency-Key"])
    assert replay.model_dump(mode="json", exclude_none=True) == posted.json()
    assert financial_counts(s["engine"]) == before
    changed = freeze_financial_push(s["ledger"], request | {"payload": payload | {"fees": []}})
    with pytest.raises(LedgerError) as caught:
        with s["structure"]._transaction(s["owner"], write=True, read_committed=True) as session:
            apply_financial_push(session, s["owner"], changed, headers["Idempotency-Key"])
    assert caught.value.code == "idempotency_conflict"
    assert financial_counts(s["engine"]) == before


def test_caller_failure_rolls_back_command_receipt_and_every_entry(ledger_setup):
    s = ledger_setup
    push = freeze_financial_push(s["ledger"], {"kind": "expense", "payload": _prepared(s)})
    before = financial_counts(s["engine"])
    with pytest.raises(RuntimeError, match="device receipt failure"):
        with s["structure"]._transaction(s["owner"], write=True, read_committed=True) as session:
            apply_financial_push(session, s["owner"], push, "fictional-device-retry")
            raise RuntimeError("device receipt failure")
    assert financial_counts(s["engine"]) == before
    assert quantities(s)["USD"] == "1000.00"
    with s["structure"]._transaction(s["owner"], write=True, read_committed=True) as session:
        applied = apply_financial_push(session, s["owner"], push, "fictional-device-retry")
    assert applied.id == push.operation_id
    assert quantities(s)["USD"] == "900.00"


def test_two_versions_remain_visible_and_terminal_cancellation_is_never_revived(ledger_setup):
    s = ledger_setup
    payload = _prepared(s)
    initial = freeze_financial_push(s["ledger"], {"kind": "expense", "payload": payload})
    with s["structure"]._transaction(s["owner"], write=True, read_committed=True) as session:
        original = apply_financial_push(session, s["owner"], initial, "fictional-original")

    stale = _revision(s, "correct", payload["id"], _correction(payload, "120.0"))
    first = _revision(s, "correct", payload["id"], _correction(payload, "130.00"))
    with s["structure"]._transaction(s["owner"], write=True, read_committed=True) as session:
        competing = apply_financial_push(session, s["owner"], first, "other-fictional-device")
    assert competing.version == 2 and quantities(s)["USD"] == "870.00"
    snapshot = financial_counts(s["engine"])

    with s["structure"]._transaction(s["owner"], write=True, read_committed=True) as session:
        with pytest.raises(LedgerError) as rejection:
            apply_financial_push(session, s["owner"], stale, "stale-fictional-device")
        assert rejection.value.code == "version_conflict"
        pair = competing_versions(session, stale)
    assert (pair.basis.version, pair.competing.version) == (1, 2)
    assert pair.competing.journals[0].reverses_journal_id == pair.basis.journals[0].id
    assert [j.lines[0].amount for j in pair.competing.journals] == ["100.00", "-130.00"]
    assert json.loads(stale.original_payload)["replacement"]["amount"] == "120.0"
    assert financial_counts(s["engine"]) == snapshot

    cancel = _revision(
        s, "cancel", payload["id"], {"expected_version": 2, "reason": "Fictional deletion"}
    )
    with s["structure"]._transaction(s["owner"], write=True, read_committed=True) as session:
        terminal = apply_financial_push(session, s["owner"], cancel, "fictional-terminal")
    assert terminal.id == original.id and terminal.status == "cancelled"
    assert quantities(s)["USD"] == "1000.00"
    snapshot = financial_counts(s["engine"])

    with s["structure"]._transaction(s["owner"], write=True, read_committed=True) as session:
        with pytest.raises(LedgerError) as rejection:
            apply_financial_push(session, s["owner"], stale, "stale-after-terminal")
        assert rejection.value.code == "version_conflict"
        tombstone = competing_versions(session, stale)
        assert tombstone.basis == pair.basis
        assert tombstone.competing.action == "cancel" and tombstone.competing.version == 3
        replay = apply_financial_push(session, s["owner"], first, "other-fictional-device")
        assert replay == competing
    assert financial_counts(s["engine"]) == snapshot

    with pytest.raises(LedgerError):
        with s["structure"]._transaction(s["owner"], write=True, read_committed=True) as session:
            apply_financial_push(session, s["owner"], initial, "fictional-reused-object-id")
    assert quantities(s)["USD"] == "1000.00"
    assert financial_counts(s["engine"]) == snapshot
