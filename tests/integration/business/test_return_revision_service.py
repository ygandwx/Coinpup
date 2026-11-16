"""Commit complete return commands and assert exact historical replay and zero-write conflicts."""

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from coinpup_api.business.allocation_return_revision_schemas import (
    AllocationReturnCancellationCreate,
    AllocationReturnCorrectionCreate,
)
from coinpup_api.business.allocation_return_revisions import BusinessAllocationReturnRevisionService
from coinpup_api.ledger.schemas import AssetUpdate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event

from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_service import correction, submit
from tests.integration.business.test_refund_service import refund
from tests.integration.business.test_return_journal_transitions import group as group
from tests.integration.business.test_return_revision_refund_guard import example
from tests.integration.business.test_return_source_sequences import pair
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def command(body, version=1, *, lines=None, day="2026-11-08"):
    return AllocationReturnCorrectionCreate.model_validate(
        dict(
            expected_version=version,
            reason="Fictional correction",
            replacement=dict(
                return_date=day,
                reason="Fictional replacement 中文",
                lines=[
                    line.model_dump(mode="json")
                    for line in (reversed(body.lines) if lines is None else lines)
                ],
            ),
        )
    )


def cancel(version):
    return AllocationReturnCancellationCreate(
        expected_version=version, reason="Fictional cancellation"
    )


def run(s, identifier, payload, key="fictional-revision", **kwargs):
    service = BusinessAllocationReturnRevisionService(s["engine"])
    method = (
        service.correct if isinstance(payload, AllocationReturnCorrectionCreate) else service.cancel
    )
    return method(s["owner"], s["ledger"], identifier, payload, key, **kwargs)


@pytest.mark.parametrize("group", [("USD", "receipt"), ("EUR", "payout")], indirect=True)
def test_actual_correct_cancel_and_permanent_raw_intent_after_archive(group):
    (s, body, original), asset = group
    change = command(body)
    raw = json.dumps(
        change.model_dump(mode="json") | {"reason": "  Fictional correction  "}
    ).encode()
    edited = run(s, original.id, change, raw_body=raw)
    assert edited.version == 2 and [m.operation_id for m in edited.members] == [
        m.operation_id for m in reversed(original.postings)
    ]
    assert all(
        m.action == "correct" and m.operation_version == 2 and m.reversal_journal_id
        for m in edited.members
    )
    for code in {asset, "USD"}:
        s["structure"].update_asset(
            s["owner"], code, AssetUpdate(expected_version=1, enabled=False)
        )
    before = snapshot(s)
    with pytest.raises(LedgerError) as rejected:
        run(s, original.id, command(body, 2), "fictional-disabled")
    assert (rejected.value.code, rejected.value.status) == ("asset_disabled", 409) and snapshot(
        s
    ) == before
    ended = run(s, original.id, cancel(2), "fictional-end")
    assert ended.version == 3 and all(
        m.action == "cancel" and m.operation_version == 3 and m.replacement is None
        for m in ended.members
    )
    with pytest.raises(LedgerError) as terminal:
        run(s, original.id, cancel(3), "fictional-after-end")
    assert terminal.value.code == "allocation_return_cancelled"
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    assert run(s, original.id, change, raw_body=raw) == edited
    assert (
        run(s, original.id, cancel(2), "fictional-end") == ended
        and submit_return(s, body) == original
    )
    with pytest.raises(LedgerError) as conflict:
        run(s, original.id, change)
    assert conflict.value.code == "idempotency_conflict" and snapshot(s) == before


def test_removed_member_rejoins_as_new_v1_without_reusing_its_prior_slot(drafts):
    s = drafts
    _, _, _, body = setup_return(s, direction="receipt", asset="EUR", count=2)
    first, second = body.lines
    original = submit_return(s, body.model_copy(update={"lines": [first]}))
    swapped = run(s, original.id, command(body, lines=[second]))
    restored = run(s, original.id, command(body, 2, lines=[first]), "fictional-swap-back")
    assert [(m.action, m.operation_version) for m in swapped.members] == [
        ("create", 1),
        ("cancel", 2),
    ]
    new, removed = restored.members
    assert (new.action, new.operation_version, new.source_return_no) == ("create", 1, 2)
    assert new.operation_id != original.postings[0].operation_id
    assert removed.operation_id == swapped.members[0].operation_id and removed.action == "cancel"


def test_effective_refunds_each_require_explicit_release_before_cancel(drafts):
    s = drafts
    body, original, cash, first = example(s)
    second = refund(s, cash.model_copy(update={"id": uuid4()}), "fictional-other-refund")
    for index, receipt in enumerate((first, second)):
        before = snapshot(s)
        for payload, code in [
            (command(body, day="2026-10-03"), "allocation_return_refund_date"),
            (cancel(1), "allocation_return_refund_reference"),
        ]:
            with pytest.raises(LedgerError) as blocked:
                run(s, original.id, payload)
            assert (blocked.value.status, blocked.value.code) == (409, code)
        assert snapshot(s) == before
        intent = correction(s, receipt, amount="5", payment_amount="5", allocation_return_ids=[])
        submit(s, receipt, "correct", intent, f"fictional-unlink-{index}")
    ended = run(s, original.id, cancel(1))
    assert ended.action == "cancel" and refund(s, cash) == first


def test_cancel_refuses_to_reprice_a_later_final_cross_asset_share(drafts):
    s = drafts
    _, first, _, _ = pair(s)
    before = snapshot(s)
    with pytest.raises(LedgerError) as blocked:
        run(s, first.id, cancel(1))
    assert (blocked.value.status, blocked.value.code) == (
        422,
        "allocation_return_ratio",
    ) and snapshot(s) == before


def test_failure_after_writing_one_members_lines_rolls_back_every_table(group):
    (s, body, original), _ = group
    before = snapshot(s)

    def injected(conn, cursor, sql, parameters, ctx, many):
        if sql.startswith("INSERT INTO journal_lines"):
            raise RuntimeError("fictional injected failure")

    event.listen(s["engine"], "after_cursor_execute", injected)
    try:
        with pytest.raises(RuntimeError, match="fictional injected"):
            run(s, original.id, command(body))
    finally:
        event.remove(s["engine"], "after_cursor_execute", injected)
    assert snapshot(s) == before
    assert run(s, original.id, command(body)).version == 2


@pytest.mark.parametrize("same_key", [False, True])
def test_concurrent_writes_commit_only_one_revision(group, same_key):
    (s, body, original), _ = group
    intent = command(body)

    def worker(key):
        try:
            return run(s, original.id, intent, key)
        except LedgerError as e:
            return e.code

    with ThreadPoolExecutor(2) as pool:
        a, b = pool.map(worker, ("fictional-one", "fictional-one" if same_key else "fictional-two"))
    if same_key:
        assert a == b and a.version == 2
    else:
        assert [x for x in (a, b) if isinstance(x, str)] == ["version_conflict"]
    ended = run(s, original.id, cancel(2), "fictional-last")
    assert ended.version == 3 and all(m.operation_version == 3 for m in ended.members)


def test_service_corrects_and_cancels_200_cross_asset_members(drafts):
    s = drafts
    _, _, _, body = setup_return(s, direction="receipt", asset="EUR", count=200)
    original = submit_return(s, body)
    change = command(body)
    edited = run(s, original.id, change)
    assert len(edited.members) == 200
    assert [m.operation_id for m in edited.members] == [
        m.operation_id for m in reversed(original.postings)
    ]
    assert all(
        m.previous.payment_amount == m.replacement.payment_amount
        and m.previous.document_amount == m.replacement.document_amount
        for m in edited.members
    )
    ended = run(s, original.id, cancel(2), "fictional-full-end")
    assert len(ended.members) == 200 and [m.previous for m in ended.members] == [
        m.replacement for m in edited.members
    ]
    assert all(m.replacement is None and m.operation_version == 3 for m in ended.members)
    before = snapshot(s)
    assert submit_return(s, body) == original and run(s, original.id, change) == edited
    assert run(s, original.id, cancel(2), "fictional-full-end") == ended and snapshot(s) == before
