"""Phase boundaries and unknown-outcome replay; these adapter tests do not prove durable commits."""

import asyncio
from threading import get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from coinpup_api.business import invoice_issuance_service as module
from coinpup_api.business.invoice_issuance_results import (
    InvoiceIssuanceComplete,
    InvoiceIssuancePending,
)
from coinpup_api.business.invoice_issuance_schemas import InvoiceIssuanceCreate
from coinpup_api.business.invoice_render_sources import InvoiceRenderSource
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.service import LedgerError

from tests.unit.business.test_invoice_issuance_results import artifacts
from tests.unit.business.test_invoice_reservation_facts import OWNER, saved, verify


@pytest.fixture
def flow(monkeypatch):
    row, _ = saved(with_logo=True, uppercase_request=True)
    verified = verify(row)
    identity = verified.identity
    data = identity.model_dump()
    pending = InvoiceIssuancePending(**data, state="pending", artifacts=())
    complete = InvoiceIssuanceComplete(**data, state="complete", artifacts=artifacts())
    source = InvoiceRenderSource(
        row.ledger_id, row.id, verified.snapshot, b"fictional original bytes"
    )
    service = module.InvoiceIssuanceService(None, store=object(), font_directory="/fictional/fonts")
    steps = []
    threads = []
    loop_thread = get_ident()

    def track(name, value):
        def wrapped(*args, **kwargs):
            steps.append(name)
            threads.append(get_ident())
            return value

        return Mock(side_effect=wrapped)

    prepared, choices = object(), object()
    reserve = track("reserve", pending)
    load = track("source", source)
    choose = track("choose", choices)
    seal = track("seal", complete)

    async def rendering(*args, **kwargs):
        steps.append("render")
        assert get_ident() == loop_thread
        return prepared

    render = AsyncMock(side_effect=rendering)
    service.reservations.reserve = reserve
    service.sources.load = load
    service.files.select_verified = choose
    service.finalization.seal = seal
    monkeypatch.setattr(module, "prepare_invoice_blobs", render)
    command = InvoiceIssuanceCreate.model_validate_json(row.request_json)

    def issue():
        return asyncio.run(
            service.issue(
                OWNER,
                row.ledger_id,
                command,
                row.idempotency_key,
                raw_body=row.request_json.encode(),
            )
        )

    return SimpleNamespace(**locals())


def test_original_key_content_source_and_verified_pair_reach_each_phase_once(flow):
    f = flow
    assert f.issue() == f.complete
    assert f.steps == ["reserve", "source", "render", "choose", "seal"]
    assert all(i != f.loop_thread for i in f.threads)
    a, kw = f.reserve.call_args
    assert a == (OWNER, f.row.ledger_id, f.command, f.row.idempotency_key)
    assert a[2] is not f.command and kw == {"raw_body": f.row.request_json.encode()}
    f.render.assert_awaited_once_with(
        f.source.snapshot.content,
        expected_sha256=f.pending.snapshot_sha256,
        font_directory="/fictional/fonts",
        store=f.service.store,
        logo_content=f.source.logo_content,
    )
    f.choose.assert_called_once_with(
        OWNER, f.row.ledger_id, f.row.id, f.prepared, store=f.service.store
    )
    f.seal.assert_called_once_with(
        OWNER, f.row.ledger_id, f.row.id, prepared=f.prepared, verified=f.choices
    )


@pytest.mark.parametrize("name", ["reserve", "load", "render", "choose", "seal"])
def test_failure_at_each_stage_never_continues_or_internally_retries(flow, name):
    f = flow
    item = getattr(f, name)
    item.side_effect = (
        MoneyError("invoice_render_failed", "Fictional")
        if name == "render"
        else LedgerError("fictional_phase_failed", 503, "Fictional")
    )
    with pytest.raises(LedgerError) as caught:
        f.issue()
    assert caught.value.code == (
        "invoice_render_failed" if name == "render" else "fictional_phase_failed"
    )
    methods = [f.reserve, f.load, f.render, f.choose, f.seal]
    stop = methods.index(item)
    assert [m.call_count for m in methods] == [int(i <= stop) for i in range(5)]


@pytest.mark.parametrize(
    "fault",
    [
        "foreign-id",
        "foreign-ledger",
        "wrong-version",
        "malformed-first",
        "wrong-source",
        "bad-readback",
        "still-pending",
    ],
)
def test_cross_intent_or_partial_success_is_not_returned(flow, fault):
    f = flow
    if fault in ("foreign-id", "foreign-ledger", "wrong-version", "malformed-first"):
        changes = {
            "foreign-id": {"id": uuid4()},
            "foreign-ledger": {"ledger_id": uuid4()},
            "wrong-version": {"document_version": 90},
            "malformed-first": {"sequence": "0"},
        }[fault]
        f.reserve.side_effect = lambda *a, **kw: f.pending.model_copy(update=changes)
    elif fault == "wrong-source":
        f.load.side_effect = lambda *a, **kw: InvoiceRenderSource(
            f.row.ledger_id, uuid4(), f.source.snapshot, None
        )
    elif fault == "bad-readback":
        f.seal.side_effect = lambda *a, **kw: f.complete.model_copy(
            update={"snapshot_sha256": "f" * 64}
        )
    else:
        f.seal.side_effect = lambda *a, **kw: f.pending
    with pytest.raises(LedgerError) as caught:
        f.issue()
    assert caught.value.code == "invoice_issuance_integrity"
    if fault not in ("bad-readback", "still-pending"):
        f.render.assert_not_awaited()


def test_retry_after_lost_completion_returns_original_receipt_without_rendering_again(flow):
    f = flow
    f.seal.side_effect = ConnectionError("fictional response lost after commit")
    with pytest.raises(ConnectionError):
        f.issue()
    f.reserve.side_effect = lambda *a, **kw: f.complete
    assert f.issue() == f.complete
    assert f.reserve.call_count == 2 and f.seal.call_count == 1 and f.render.await_count == 1
    assert f.reserve.call_args_list[0] == f.reserve.call_args_list[1]


def test_cancellation_is_not_converted_to_success_or_a_new_attempt(flow):
    f = flow
    f.render.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        f.issue()
    f.choose.assert_not_called()
    f.seal.assert_not_called()
    assert f.reserve.call_count == 1
