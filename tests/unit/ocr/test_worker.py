"""Fictional stores/queues isolate lifecycle decisions, never claim actual SDK execution."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from coinpup_api.ledger.service import LedgerError
from coinpup_api.ocr import worker
from coinpup_api.ocr.contracts import Lease, canonical_json
from coinpup_api.ocr.isolation import IsolationError, ProcessResult
from coinpup_api.ocr.runtime import processing_configuration
from sqlalchemy.exc import SQLAlchemyError

from tests.unit.ocr.test_candidates import statement


@pytest.fixture
def context():
    data = b"Fictional file bytes; fake recognizer only"
    owner = uuid4()
    config = {"lease_seconds": 120, "retry_seconds": 0, "processing": processing_configuration()}
    lease = Lease(
        owner,
        uuid4(),
        uuid4(),
        uuid4(),
        1,
        uuid4(),
        datetime.now(UTC) + timedelta(seconds=120),
        canonical_json(config, 16384),
        uuid4().hex,
        hashlib.sha256(data).hexdigest(),
        len(data),
        "application/pdf",
    )
    events = []

    class Queue:
        def claim(self, value):
            assert value == owner
            events.append("claim")
            return lease

        def renew(self, value):
            assert value.token == lease.token and value.generation == lease.generation
            events.append("renew")
            return replace(value, lease_until=value.lease_until + timedelta(seconds=1))

        def finish(self, value, completion):
            events.append("finish")
            assert value.lease_until > lease.lease_until
            assert len(completion.candidates) == 1
            return SimpleNamespace(state="succeeded")

        def fail(self, value, code, *, retryable=False):
            events.append(("fail", code, retryable))
            return SimpleNamespace(state="pending" if retryable else "failed")

    class Store:
        def open_blob(self, *identity):
            assert identity == (lease.blob_key, lease.sha256, lease.byte_size)
            events.append("open")
            return BytesIO(data)

    result = statement("10.00", ocr=(0,))
    return SimpleNamespace(
        queue=Queue(),
        store=Store(),
        owner=owner,
        lease=lease,
        config=config,
        events=events,
        data=data,
        result=result,
    )


def isolated(context):
    def run(request, budget, *, heartbeat):
        assert request.keys() == {"version", "action", "source", "media_type", "processing"}
        assert str(context.lease.token) not in json.dumps(request)
        path = Path(request["source"]["path"])
        assert path.read_bytes() == context.data
        assert path.suffix == ".pdf" and len(path.stem) == 32
        assert budget.address_space_bytes == 4 * 1024**3
        assert heartbeat() is True
        context.path = path
        context.events.append("isolate")
        return ProcessResult(json.dumps(context.result).encode(), 0, 1)

    return run


def execute(s, **kwargs):
    return worker.run_once(
        s.queue, s.store, s.owner, isolate=kwargs.pop("isolate", isolated(s)), **kwargs
    )


def test_claim_then_private_source_compute_cleanup_and_only_then_finish(context):
    s = context
    original = s.queue.finish

    def finish(*args):
        assert not s.path.exists() and not s.path.parent.exists()
        return original(*args)

    s.queue.finish = finish
    assert execute(s) == "succeeded"
    assert s.events == ["claim", "renew", "open", "renew", "isolate", "renew", "finish"]


def test_idle_never_touches_storage_or_processor(context):
    s = context
    s.queue.claim = lambda _: None
    assert execute(s) == "idle" and s.events == []


def test_heartbeat_throttles_and_uses_monotonic_time(context):
    s, clock = context, [5.0]
    pulse = worker._Pulse(s.queue, s.lease, 40, lambda: clock[0])
    assert pulse() and pulse() and pulse()
    assert s.events == ["renew"]
    clock[0] = 46.0
    assert pulse() and s.events == ["renew", "renew"]
    assert pulse(force=True) and s.events == ["renew"] * 3


@pytest.mark.parametrize("mode", ["archived", "lost", "uncertain"])
def test_renewal_failure_never_triggers_failure_or_completion_write(context, mode):
    s = context

    def renew(_):
        if mode == "archived":
            return SimpleNamespace(state="failed")
        if mode == "lost":
            raise LedgerError("ocr_lease_lost", 409, "Fictional lost lease")
        raise SQLAlchemyError("Fictional private database information")

    s.queue.renew = renew
    assert execute(s) == "cancelled" and s.events == ["claim"]


def test_lost_renewal_after_child_returns_discards_late_result_and_cleans_source(context):
    s, run = context, isolated(context)

    def late(*args, **kwargs):
        output = run(*args, **kwargs)
        s.queue.renew = lambda _: SimpleNamespace(state="failed")
        return output

    assert execute(s, isolate=late) == "cancelled"
    assert not s.path.parent.exists()
    assert not any(item == "finish" or isinstance(item, tuple) for item in s.events)


@pytest.mark.parametrize(
    "code,retry",
    [
        ("processor_timeout", True),
        ("processor_unavailable", True),
        ("resource_limit", False),
        ("processing_failed", False),
    ],
)
def test_process_errors_use_bounded_queue_failures(context, code, retry):
    s = context

    def fail(*_args, **_kwargs):
        raise IsolationError(code)

    assert execute(s, isolate=fail) == ("pending" if retry else "failed")
    assert s.events[-1] == ("fail", code, retry)


def test_unconfirmed_process_cleanup_stops_worker_without_finishing_or_failing(context):
    s = context

    def fail(*_args, **_kwargs):
        raise IsolationError("processing_cleanup_failed")

    with pytest.raises(IsolationError, match="cleanup"):
        execute(s, isolate=fail)
    assert not any(item == "finish" or isinstance(item, tuple) for item in s.events)


@pytest.mark.parametrize("mode", ["changed", "short", "oversized"])
def test_copy_checks_identity_and_limit_before_child(context, mode):
    s = context
    if mode == "oversized":
        lease = replace(s.lease, byte_size=21 * 1024**2)
        s.queue.claim = lambda _: lease
    else:
        s.store.open_blob = lambda *_: BytesIO(b"x" * (len(s.data) if mode == "changed" else 1))
    assert execute(s) == "failed"
    assert s.events[-1] == ("fail", "source_unavailable", False)
    assert "isolate" not in s.events


def test_config_drift_is_rejected_before_file_access(context):
    s = context
    s.config["processing"]["prepare"]["dpi"] = 200
    s.queue.claim = lambda _: replace(s.lease, configuration_json=canonical_json(s.config, 16384))
    assert execute(s) == "failed"
    assert s.events == [("fail", "configuration_invalid", False)]


@pytest.mark.parametrize(
    "error",
    [
        SQLAlchemyError("Fictional uncertain commit"),
        LedgerError("constraint_conflict", 409, "Fictional conflict"),
    ],
)
def test_uncertain_or_rejected_completion_is_never_followed_by_failure_write(context, error):
    s = context

    def finish(*_):
        raise error

    s.queue.finish = finish
    with pytest.raises(type(error)):
        execute(s)
    assert not s.path.parent.exists() and not any(isinstance(item, tuple) for item in s.events)


def test_late_completion_cas_returns_cancelled(context):
    s = context

    def finish(*_):
        raise LedgerError("ocr_lease_lost", 409, "Fictional lost lease")

    s.queue.finish = finish
    assert execute(s) == "cancelled"
    assert not any(isinstance(item, tuple) for item in s.events)


def test_non_linux_cli_stops_before_database(monkeypatch, capsys):
    monkeypatch.setattr(worker.sys, "platform", "win32")
    monkeypatch.setattr(worker, "Database", lambda *_: pytest.fail("Started database"))
    assert worker.main(["--once"]) == 1
    assert "requires Linux" in capsys.readouterr().err


@pytest.mark.parametrize(
    "reason,code,retry",
    [
        ("engine_unavailable", "processor_unavailable", True),
        ("engine_initialization_failed", "processor_unavailable", True),
        ("engine_limit", "resource_limit", False),
        ("engine_recognition_failed", "invalid_document", False),
    ],
)
def test_engine_failures_are_stable_and_cleaned_before_queue_write(context, reason, code, retry):
    s = context
    s.result.update(status="failed", reason=reason)
    assert execute(s) == ("pending" if retry else "failed")
    assert not s.path.parent.exists()
    assert s.events[-1] == ("fail", code, retry)


def test_source_cleanup_failure_stops_before_result_write(context, monkeypatch):
    s, factory = context, worker.tempfile.TemporaryDirectory

    def directory(*args, **kwargs):
        original = factory(*args, **kwargs)

        def cleanup():
            original.cleanup()
            raise OSError("Fictional cleanup confirmation error")

        return SimpleNamespace(name=original.name, cleanup=cleanup)

    monkeypatch.setattr(worker.tempfile, "TemporaryDirectory", directory)
    with pytest.raises(IsolationError) as caught:
        execute(s)
    assert caught.value.code == "processing_cleanup_failed"
    assert not any(item == "finish" or isinstance(item, tuple) for item in s.events)


def test_rejected_failure_write_is_not_replaced_with_another_failure(context):
    s = context
    s.result.update(status="failed", reason="engine_unavailable")
    writes = []

    def fail(_lease, code, **_options):
        writes.append(code)
        raise LedgerError("constraint_conflict", 409, "Fictional rejected transition")

    s.queue.fail = fail
    with pytest.raises(LedgerError):
        execute(s)
    assert writes == ["processor_unavailable"]
