"""Authenticated device pushes, immutable financial outcomes and explicit user decisions."""

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import anyio
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.ledger.idempotency import _canonical_hash
from coinpup_api.ledger.models import CommandReceipt
from coinpup_api.sync.device_models import DeviceRevision
from coinpup_api.sync.devices import DeviceRegistry
from coinpup_api.sync.financial_push import MAX_FINANCIAL_PUSH_BYTES
from coinpup_api.sync.financial_sync import FinancialSyncService
from coinpup_api.sync.financial_sync_models import FinancialPushRecord, FinancialResolutionRecord
from sqlalchemy import event, insert, select, text
from sqlalchemy.exc import IntegrityError

from tests.integration.conftest import financial_device_push_migration
from tests.integration.ledger.legacy_v1_receipts import create_legacy_v1_receipts
from tests.integration.ledger.test_posting_service_database import financial_counts, quantities
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.sync.test_financial_push_core import _correction, _prepared

pytestmark = pytest.mark.integration
DEVICES = "/api/v1/sync/devices"


def _register(client, *, current=False):
    base = f"{DEVICES}/{uuid4()}"
    start = (
        client.get("/api/v1/changes/start").json() if current else {"cursor": "0", "witness": None}
    )
    response = client.put(base, json=start)
    assert response.status_code == 200, response.text
    assert client.get(base + "/checkpoint").json() == start
    return base, start


def _path(base, intent=None):
    return base + "/financial-pushes/" + str(intent or uuid4())


def _request(scope, checkpoint, key, kind, payload, operation_id=None):
    command = {"kind": kind, "payload": payload}
    if operation_id is not None:
        command["operation_id"] = operation_id
    return {
        "ledger_id": str(scope["ledger"]),
        "idempotency_key": key,
        "checkpoint": checkpoint,
        "command": command,
    }


def _put(client, path, body, expected=200):
    result = client.put(path, json=body)
    assert result.status_code == expected, result.text
    return result.json()


@pytest.fixture
def legacy_device_receipts(structure_database):
    return create_legacy_v1_receipts(*structure_database, stable_create_ids=True)


def _linked_receipt(engine, response):
    push, ledger = FinancialPushRecord, CommandReceipt
    key = UUID(response["intent_id"])
    with engine.connect() as connection:
        return connection.execute(
            select(push.response["receipt"] == ledger.response, ledger.hash_version)
            .select_from(push)
            .join(ledger, (ledger.ledger_id == push.ledger_id) & (ledger.key == push.receipt_key))
            .where(push.intent_id == key)
        ).one()


def test_device_receipts_match_all_supported_legacy_creation_and_revision_shapes(
    legacy_device_receipts, authenticated_client
):
    s = legacy_device_receipts
    client, engine, _ = authenticated_client
    base, anchor = _register(client)
    for name, case in s["cases"].items():
        kind = "expense" if name == "revision_original" else name
        operation = case["response"]["id"] if case["status"] == 200 else None
        body = _request(s, anchor, case["key"], kind, case["body"], operation)
        path = _path(base)
        result = _put(client, path, body)
        assert result["receipt"] == case["response"]
        assert _linked_receipt(engine, result) == (True, 1)
        assert client.get(path).json() == result == _put(client, path, body)
    assert s["snapshot"](engine) == s["before_upgrade"]


def test_original_replay_survives_checkpoint_progress_and_retirement(
    ledger_setup, authenticated_client
):
    s = ledger_setup
    client, engine, _ = authenticated_client
    payload = _prepared(s)
    base, original_checkpoint = _register(client, current=True)
    path = _path(base)
    other, _ = _register(client)
    expense = _request(s, original_checkpoint, "fictional-http-push", "expense", payload)
    assert client.get(path).status_code == 404
    assert (
        client.put(path, json=expense, headers={"x-csrf-token": "fictional-invalid"}).status_code
        == 403
    )
    altered = expense | {
        "checkpoint": original_checkpoint
        | {
            "witness": original_checkpoint["witness"] | {"entity_id": str(uuid4())},
        }
    }
    assert _put(client, path, altered, 409)["detail"]["code"] == "sync_checkpoint_conflict"
    assert quantities(s)["USD"] == "1000.00"

    response = _put(client, path, expense)
    assert response["outcome"] == "applied"
    assert response["receipt"]["amount"] == "100.00" and response["operation_id"] == payload["id"]
    assert _linked_receipt(engine, response) == (True, 2)
    had = financial_counts(engine)
    assert client.get(path).headers["cache-control"] == "no-store"
    assert client.get(path).json() == response
    assert client.get(_path(other, response["intent_id"])).status_code == 404

    after = original_checkpoint["cursor"]
    through = client.get("/api/v1/changes/start").json()["cursor"]
    options = {"after": after, "through": through, "limit": 10}
    received = client.get("/api/v1/changes", params=options).json()
    ack = client.post(
        base + "/acknowledgements",
        json=options | {"prior": original_checkpoint["witness"], "received": received},
    )
    assert ack.status_code == 200, ack.text
    assert client.get(base + "/checkpoint").json()["cursor"] != after
    assert _put(client, path, expense) == response
    assert _put(client, _path(base), expense, 409)["detail"]["code"] == "sync_checkpoint_conflict"
    _put(client, base + "/retirement", None)
    assert _put(client, path, expense) == response
    altered_key = expense | {"idempotency_key": "fictional-wrong-key"}
    assert _put(client, path, altered_key, 409)["detail"]["code"] == "idempotency_conflict"
    assert _put(client, _path(base), expense, 409)["detail"]["code"] == "sync_device_retired"
    assert _put(client, _path(other), expense | {"ledger_id": str(uuid4())}, 404)
    assert quantities(s)["USD"] == "900.00" and financial_counts(engine) == had
    client.cookies.clear()
    assert client.get(path).status_code == client.put(path, json=expense).status_code == 401


def test_push_stream_stops_at_cap_and_original_boundary_command_applies_once(
    ledger_setup, authenticated_client
):
    s = ledger_setup
    client, engine, _ = authenticated_client
    base, checkpoint = _register(client)
    path = _path(base)
    body = _request(s, checkpoint, "fictional-cap-boundary", "expense", _prepared(s))
    encoded = json.dumps(body).encode()
    exact = encoded.ljust(MAX_FINANCIAL_PUSH_BYTES)

    async def oversized_stream():
        frames = iter((exact[:65536], exact[65536:], b" "))
        sent = []

        async def receive():
            # There is more transport data after the over-limit frame. It must remain unread.
            chunk = next(frames, None)
            assert chunk is not None
            return {"type": "http.request", "body": chunk, "more_body": True}

        async def send(message):
            sent.append(message)

        await client.app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "method": "PUT",
                "path": path,
                "scheme": "http",
                "root_path": "",
                "query_string": b"",
                "http_version": "1.1",
                "headers": [(b"content-type", b"application/json")],
                "server": ("localhost", 8000),
                "client": ("127.0.0.1", 10000),
            },
            receive,
            send,
        )
        return sent

    streamed = anyio.run(oversized_stream)
    assert streamed[0]["status"] == 422
    rejected = {
        "detail": {
            "code": "invalid_financial_push",
            "message": "Expected a bounded financial push command.",
        }
    }
    assert json.loads(b"".join(message.get("body", b"") for message in streamed)) == rejected

    headers = {"content-type": "application/json"}
    before = financial_counts(engine)
    assert client.put(path, content=exact + b" ", headers=headers).json() == rejected
    assert client.get(path).status_code == 404
    assert financial_counts(engine) == before

    result = client.put(path, content=exact, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["receipt"]["amount"] == "100.00"
    assert client.put(path, content=exact + b" ", headers=headers).json() == rejected
    assert client.put(path, content=encoded, headers=headers).content == result.content
    assert client.get(path).content == result.content
    assert (
        client.put(path, json=body | {"idempotency_key": "fictional-changed-key"}).status_code
        == 409
    )
    assert quantities(s)["USD"] == "900.00"


def test_conflict_keeps_original_and_fixed_versions_after_newer_cancellation(
    ledger_setup, authenticated_client
):
    s = ledger_setup
    client, engine, _ = authenticated_client
    payload = _prepared(s)
    first, anchor = _register(client)
    peer, peer_anchor = _register(client)
    _put(client, _path(first), _request(s, anchor, "original-expense", "expense", payload))
    identity = payload["id"]
    proposed = _correction(payload, "120.0") | {"reason": " Fictional offline reason "}
    revised = _put(
        client,
        _path(first),
        _request(
            s, anchor, "accepted-revision", "correct", _correction(payload, "130.00"), identity
        ),
    )
    assert _linked_receipt(engine, revised) == (True, 2)
    expected = financial_counts(engine)
    path = _path(peer)
    offline = _request(s, peer_anchor, "offline-revision", "correct", proposed, identity)
    conflict = _put(client, path, offline)
    assert conflict["outcome"] == "conflict"
    assert json.loads(conflict["original_payload"]) == proposed
    assert conflict["basis"]["version"] == 1 and conflict["competing"]["version"] == 2
    assert conflict["competing"]["journals"][-1]["lines"][0]["amount"] == "-130.00"
    assert quantities(s)["USD"] == "870.00" and financial_counts(engine) == expected
    assert client.get(path + "/resolution").json() is None
    stale_choice = _put(client, path + "/resolution", {"expected_current_version": 1}, 409)
    assert stale_choice["detail"]["code"] == "version_conflict"
    kept = _put(client, path + "/resolution", {"expected_current_version": 2})
    assert kept["accepted_version"] == 2
    late_path = _path(peer)
    late_push = offline | {"idempotency_key": "fictional-late-decision"}
    late_conflict = _put(client, late_path, late_push)
    assert late_conflict["basis"] == conflict["basis"]
    assert late_conflict["competing"] == conflict["competing"]

    terminal = _put(
        client,
        _path(first),
        _request(
            s,
            anchor,
            "explicit-cancellation",
            "cancel",
            {"expected_version": 2, "reason": "Fictional terminal deletion"},
            identity,
        ),
    )
    assert terminal["receipt"]["status"] == "cancelled"
    assert _linked_receipt(engine, terminal) == (True, 2)
    expected = financial_counts(engine)
    assert _put(client, path, offline) == client.get(path).json() == conflict
    assert _put(client, path + "/resolution", {"expected_current_version": 2}) == kept
    assert client.get(path + "/resolution").json() == kept
    assert _put(client, path + "/resolution", {"expected_current_version": 3}, 409)
    late_choice = _put(client, late_path + "/resolution", {"expected_current_version": 2}, 409)
    assert late_choice["detail"]["code"] == "version_conflict"
    assert client.get(late_path + "/resolution").json() is None
    kept_latest = _put(client, late_path + "/resolution", {"expected_current_version": 3})
    assert kept_latest["accepted_version"] == 3
    assert client.get(late_path).json() == late_conflict
    tombstone = _put(client, _path(peer), offline | {"idempotency_key": "second-stale-revision"})
    assert tombstone["competing"]["action"] == "cancel"
    fresh = _request(
        s,
        peer_anchor,
        "terminal-correction",
        "correct",
        _correction(payload, "140.00", 3),
        identity,
    )
    assert _put(client, _path(peer), fresh, 409)["detail"]["code"] == "operation_cancelled"
    assert _put(client, _path(peer), _request(s, peer_anchor, "reused-id", "expense", payload), 409)
    assert quantities(s)["USD"] == "1000.00" and financial_counts(engine) == expected
    _put(client, peer + "/retirement", None)
    assert _put(client, late_path + "/resolution", {"expected_current_version": 3}) == kept_latest
    assert _put(client, late_path, late_push) == late_conflict

    with pytest.raises(IntegrityError) as failure, engine.begin() as session:
        session.execute(text("DELETE FROM sync_financial_conflict_resolutions"))
    assert failure.value.orig.diag.constraint_name == "ck_sync_financial_resolution_immutable"
    with pytest.raises(IntegrityError) as failure, engine.begin() as session:
        session.execute(text("UPDATE sync_financial_pushes SET request_body = '{}'"))
    assert failure.value.orig.diag.constraint_name == "ck_sync_financial_push_immutable"
    with (
        pytest.raises(IntegrityError) as failure,
        engine.begin() as session,
        Operations.context(MigrationContext.configure(session)),
    ):
        financial_device_push_migration()["downgrade"]()
    assert failure.value.orig.diag.constraint_name == "ck_sync_financial_push_downgrade"


def test_database_rejects_stale_and_retired_device_writes(ledger_setup, authenticated_client):
    s = ledger_setup
    client, engine, owner = authenticated_client
    first, anchor = _register(client)
    peer, peer_anchor = _register(client)
    payload = _prepared(s)
    identity = payload["id"]
    _put(client, _path(first), _request(s, anchor, "fictional-initial", "expense", payload))
    _put(
        client,
        _path(first),
        _request(
            s, anchor, "fictional-update", "correct", _correction(payload, "130.00"), identity
        ),
    )
    path = _path(peer)
    body = _request(
        s, peer_anchor, "fictional-conflict", "correct", _correction(payload, "120.00"), identity
    )
    outcome = _put(client, path, body)
    assert outcome["outcome"] == "conflict"
    device, intent = UUID(outcome["device_id"]), UUID(outcome["intent_id"])
    new_intent = uuid4()
    query = select(FinancialPushRecord.__table__).where(FinancialPushRecord.intent_id == intent)
    with engine.connect() as connection:
        original = dict(connection.execute(query).mappings().one())
    push = insert(FinancialPushRecord).values(
        original | {"intent_id": new_intent, "response": outcome | {"intent_id": str(new_intent)}}
    )
    choice = insert(FinancialResolutionRecord).values(
        owner_id=owner, device_id=device, intent_id=intent, accepted_version=2
    )
    state = (
        select(DeviceRevision.state)
        .where(DeviceRevision.owner_id == owner, DeviceRevision.device_id == device)
        .order_by(DeviceRevision.version.desc())
        .limit(1)
    )
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as stale:
        with stale.begin():
            assert stale.scalar(state) == "registered"
            assert _put(client, peer + "/retirement", None)["state"] == "retired"
            assert stale.scalar(state) == "registered"
            for statement in (push, choice):
                with pytest.raises(IntegrityError) as failure, stale.begin_nested():
                    stale.execute(statement)
                assert failure.value.orig.diag.constraint_name == "ck_sync_protocol_isolation"
    for statement, constraint in [
        (push, "ck_sync_financial_push_retired"),
        (choice, "ck_sync_financial_resolution_retired"),
    ]:
        with engine.connect().execution_options(isolation_level="READ COMMITTED") as fresh:
            with pytest.raises(IntegrityError) as failure, fresh.begin():
                assert fresh.scalar(state) == "retired"
                fresh.execute(statement)
            assert failure.value.orig.diag.constraint_name == constraint
    assert client.get(path + "/resolution").json() is None
    assert client.get(_path(peer, new_intent)).status_code == 404
    assert _put(client, path, body) == outcome


def test_unavailable_device_receipt_rolls_back_the_existing_financial_command(
    ledger_setup, authenticated_client
):
    s = ledger_setup
    client, engine, _ = authenticated_client
    payload = _prepared(s)
    base, anchor = _register(client)
    path = _path(base)
    original = _request(s, anchor, "retry-after-no-receipt", "expense", payload)
    before = financial_counts(engine)

    def refuse_sync_receipt(_connection, _cursor, statement, _values, _context, _batch):
        if statement.startswith("INSERT INTO sync_financial_pushes"):
            raise RuntimeError("fictional receipt write failure")

    event.listen(engine, "before_cursor_execute", refuse_sync_receipt)
    try:
        with pytest.raises(RuntimeError, match="fictional receipt write failure"):
            client.put(path, json=original)
    finally:
        event.remove(engine, "before_cursor_execute", refuse_sync_receipt)
    assert financial_counts(engine) == before and quantities(s)["USD"] == "1000.00"
    assert client.get(path).status_code == 404
    assert _put(client, path, original)["outcome"] == "applied"
    assert quantities(s)["USD"] == "900.00"


def test_database_rejects_a_device_envelope_with_changed_receipt_evidence(
    ledger_setup, authenticated_client
):
    s = ledger_setup
    client, engine, owner = authenticated_client
    base, anchor = _register(client)
    body = _request(s, anchor, "fictional-immutable-receipt", "expense", _prepared(s))
    original = _put(client, _path(base), body)
    before = financial_counts(engine)
    assert original["outcome"] == "applied"
    assert original["receipt"]["amount"] == "100.00"
    query = select(FinancialPushRecord.__table__).where(
        FinancialPushRecord.intent_id == UUID(original["intent_id"])
    )
    with engine.connect() as connection:
        stored = dict(connection.execute(query).mappings().one())
    intent = uuid4()
    envelope = original | {"intent_id": str(intent)}
    correct_request_hash = _canonical_hash(
        {
            "protocol": "financial_push_v1",
            "owner": str(owner),
            "device": original["device_id"],
            "intent": str(intent),
            "body": body,
        }
    )
    row = stored | {"intent_id": intent, "request_hash": correct_request_hash}
    for response, constraint in [
        (
            envelope | {"receipt": envelope["receipt"] | {"amount": "999.00"}},
            "ck_sync_financial_push_receipt_fact",
        ),
        (
            envelope | {"device_id": str(uuid4())},
            "ck_sync_financial_push_response_identity",
        ),
    ]:
        with pytest.raises(IntegrityError) as failure, engine.begin() as connection:
            connection.execute(insert(FinancialPushRecord).values(row | {"response": response}))
        assert failure.value.orig.diag.constraint_name == constraint
    assert client.get(_path(base, intent)).status_code == 404
    again = _put(client, _path(base, intent), body)
    assert again == envelope
    assert _linked_receipt(engine, again) == (True, 2)
    assert financial_counts(engine) == before


@pytest.mark.slow
def test_two_concurrent_devices_keep_evidence_of_the_losing_correction(ledger_setup):
    s = ledger_setup
    sync = FinancialSyncService(s["engine"])
    registry = DeviceRegistry(s["engine"])
    owner = s["owner"]
    bodies = []
    for amount in ("120.00", "130.00"):
        device = uuid4()
        registry.register(owner, device)
        bodies.append((device, uuid4(), amount))
    payload = _prepared(s)
    original = _request(s, {"cursor": "0", "witness": None}, "fictional-start", "expense", payload)
    sync.submit(owner, bodies[0][0], uuid4(), json.dumps(original).encode())

    def update(device, intent, amount):
        body = _request(
            s,
            {"cursor": "0", "witness": None},
            amount,
            "correct",
            _correction(payload, amount),
            payload["id"],
        )
        return sync.submit(owner, device, intent, json.dumps(body).encode())

    with ThreadPoolExecutor(max_workers=2) as pool:
        work = [pool.submit(update, *args) for args in bodies]
        results = {item.outcome: item for item in (call.result() for call in work)}
    assert set(results) == {"applied", "conflict"}
    receipt, conflict = results["applied"].receipt, results["conflict"]
    assert receipt.version == conflict.competing.version == 2
    assert conflict.basis.version == 1
    assert (
        json.loads(conflict.original_payload)["replacement"]["amount"]
        != receipt.latest_posting.amount
    )
