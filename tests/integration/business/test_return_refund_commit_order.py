"""Both legal writers contend for the real database lock. Only one final state can commit."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident
from time import monotonic, sleep

import pytest
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event, text

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_service import correction, submit
from tests.integration.business.test_return_revision_refund_guard import example
from tests.integration.business.test_return_revision_service import cancel, run
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def blocked_writers(engine, funcs, first, finish="commit"):
    second = "return" if first == "refund" else "refund"
    held, release, asking = Event(), Event(), Event()
    tids = {}
    pids = {}

    def invoke(side):
        tids[side] = get_ident()
        return funcs[side]()

    def before_commit(conn):
        if get_ident() == tids.get(first):
            pids[first] = conn.connection.driver_connection.info.backend_pid
            held.set()
            assert release.wait(15), "first writer was not released"

    def before_statement(conn, cursor, statement, parameters, context, executemany):
        if get_ident() == tids.get(second) and "pg_advisory_xact_lock" in statement:
            pids[second] = conn.connection.driver_connection.info.backend_pid
            asking.set()

    event.listen(engine, finish, before_commit)
    event.listen(engine, "before_cursor_execute", before_statement)
    try:
        with ThreadPoolExecutor(2) as pool:
            winner = pool.submit(invoke, first)
            try:
                assert held.wait(10)
                loser = pool.submit(invoke, second)
                assert asking.wait(10)
                with engine.connect() as watch:
                    until = monotonic() + 8
                    while pids[first] not in watch.scalar(
                        text("SELECT pg_blocking_pids(:pid)"), {"pid": pids[second]}
                    ):
                        assert monotonic() < until, (
                            "second writer did not wait for the first database transaction"
                        )
                        sleep(0.01)
            finally:
                release.set()
            results = {}
            for side, job in [(first, winner), (second, loser)]:
                try:
                    results[side] = job.result(timeout=12)
                except LedgerError as error:
                    results[side] = error
    finally:
        event.remove(engine, "before_cursor_execute", before_statement)
        event.remove(engine, finish, before_commit)
    return results


@pytest.mark.parametrize(("first", "direction"), [("refund", "receipt"), ("return", "payout")])
def test_only_one_writer_commits_when_linking_a_refund_races_with_return_cancellation(
    drafts, authenticated_client, first, direction
):
    s = drafts
    client, engine, _ = authenticated_client
    _, returned, _, refund = example(s, direction, linked=False)
    edit = correction(
        s, refund, amount="5", payment_amount="5", allocation_return_ids=[str(returned.id)]
    )

    def link():
        return submit(s, refund, "correct", edit, "fictional-competing-refund-reference")

    def end():
        return run(s, returned.id, cancel(1), "fictional-competing-return-cancellation")

    funcs = {"return": end, "refund": link}
    second = "return" if first == "refund" else "refund"
    root = f"/api/v1/ledgers/{s['ledger']}"
    refund_url = f"{root}/business-refunds/{refund.id}"
    return_url = f"{root}/business-allocation-returns/{returned.id}"

    def read(path, **kwargs):
        out = client.get(path, **kwargs)
        assert out.status_code == 200
        return out

    def cash():
        return read(root + "/balances", params={"account_id": str(refund.account_id)}).json()

    originals = {p: read(p).content for p in (return_url, refund_url)}
    prior_cash = cash()
    pair = blocked_writers(engine, funcs, first)
    accepted, rejected = pair[first], pair[second]
    assert isinstance(rejected, LedgerError)
    code = "allocation_return_refund_reference" if first == "refund" else "operation_cancelled"
    assert (rejected.status, rejected.code) == (409, code) and accepted.version == 2
    current = read(refund_url + "/current").json()
    ret = read(return_url + "/current").json()
    if first == "refund":
        assert (current["version"], current["fact"]["allocation_return_ids"]) == (
            2,
            [str(returned.id)],
        )
        assert (ret["version"], ret["status"]) == (1, "active")
    else:
        assert (current["version"], current["fact"]["allocation_return_ids"]) == (1, [])
        assert (ret["version"], ret["status"]) == (2, "cancelled")
    assert [r["version"] for r in read(refund_url + "/history").json()] == list(
        range(1, current["version"] + 1)
    )
    assert [r["version"] for r in read(return_url + "/history").json()] == list(
        range(1, ret["version"] + 1)
    )
    after = snapshot(s)
    assert funcs[first]() == accepted
    with pytest.raises(LedgerError) as again:
        funcs[second]()
    assert (again.value.status, again.value.code) == (409, code) and cash() == prior_cash
    assert all(read(p).content == v for p, v in originals.items()) and snapshot(s) == after


@pytest.mark.parametrize("first", ["refund", "return"])
@pytest.mark.parametrize("direction", ["receipt", "payout"])
def test_cancellation_requires_committed_explicit_detachment_and_preserves_old_evidence(
    drafts,
    authenticated_client,
    first,
    direction,
):
    s = drafts
    client, engine, _ = authenticated_client
    _, returned, _, refund = example(s, direction, linked=True)
    root = f"/api/v1/ledgers/{s['ledger']}"
    refunds = f"{root}/business-refunds/{refund.id}"
    returns = f"{root}/business-allocation-returns/{returned.id}"

    def read(p, **opts):
        reply = client.get(p, **opts)
        assert reply.status_code == 200, reply.text
        return reply

    originals = {p: read(p).content for p in [refunds, returns]}
    assert read(refunds).json()["receipt"]["allocation_return_ids"] == [str(returned.id)]

    def balance():
        return read(root + "/balances", params={"account_id": str(refund.account_id)}).json()

    original_balance = balance()
    body = correction(s, refund, amount="5", payment_amount="5", allocation_return_ids=[])

    def detach():
        return submit(s, refund, "correct", body, "fictional-explicit-detach")

    key = "fictional-cancel-after-explicit-detach"

    def end():
        return run(s, returned.id, cancel(1), key)

    # Hold the actual first transaction at commit, or at rollback if the still-linked
    # return must be rejected. PostgreSQL itself must report the second PID waiting.
    pair = blocked_writers(
        engine,
        {"refund": detach, "return": end},
        first,
        finish="commit" if first == "refund" else "rollback",
    )
    edited, result = pair["refund"], pair["return"]
    assert (edited.version, edited.action) == (2, "correct")

    def receipts():
        return [
            r
            for r in snapshot(s)["command_receipts"]
            if r["ledger_id"] == s["ledger"] and r["key"] == key
        ]

    if first == "return":
        assert isinstance(result, LedgerError) and (result.status, result.code) == (
            409,
            "allocation_return_refund_reference",
        )
        assert not receipts() and read(returns + "/current").json()["status"] == "active"
        result = end()
    assert (result.version, result.action) == (2, "cancel")
    assert len(receipts()) == 1 and receipts()[0]["response_status"] == 200
    assert read(returns + "/current").json()["status"] == "cancelled"
    assert read(refunds + "/current").json()["fact"]["allocation_return_ids"] == []
    assert [(r["version"], r["action"]) for r in read(refunds + "/history").json()] == [
        (1, "create"),
        (2, "correct"),
    ]
    assert [(r["version"], r["action"]) for r in read(returns + "/history").json()] == [
        (1, "create"),
        (2, "cancel"),
    ]
    assert all(read(p).content == v for p, v in originals.items())
    assert balance() == original_balance
    after = snapshot(s)
    assert detach() == edited and end() == result and snapshot(s) == after
