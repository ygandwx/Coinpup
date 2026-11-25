from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from coinpup_api.reminders.mail_attempt import MailDisposition as D
from coinpup_api.reminders.mail_attempt import MailResult
from coinpup_api.reminders.mail_lifecycle import AttemptConflict, AttemptState, MailAttempt

NOW = datetime(2026, 11, 1, 1, tzinfo=UTC)
LEASE = timedelta(minutes=2)
ID, TOKEN = UUID(int=100), UUID(int=1)


def leased():
    return MailAttempt(ID).claim(NOW, TOKEN, NOW + LEASE)


@pytest.mark.parametrize("outcome", list(D))
def test_late_or_stale_worker_cannot_replace_authoritative_evidence(outcome):
    a = leased()
    for at, generation, token in [(NOW, 0, TOKEN), (NOW, 1, UUID(int=2)), (NOW + LEASE, 1, TOKEN)]:
        with pytest.raises(AttemptConflict):
            a.record(at, generation, token, MailResult(outcome))
    unknown = a.expire(NOW + LEASE)
    assert unknown.state == AttemptState.UNKNOWN and unknown.lease_token is None
    for op in (
        lambda: unknown.claim(NOW, TOKEN, NOW + LEASE),
        lambda: unknown.retry_explicitly(NOW),
        lambda: unknown.record(NOW, 1, TOKEN, MailResult(outcome)),
    ):
        with pytest.raises(AttemptConflict):
            op()
    assert a.state == AttemptState.SENDING and unknown.delivery_id == ID


@pytest.mark.parametrize("target", [AttemptState.VERIFIED_RECEIVED, AttemptState.STOPPED])
def test_unknown_must_be_resolved_explicitly_with_a_reason_and_never_becomes_reclaimable(target):
    u = leased().record(NOW, 1, TOKEN, MailResult(D.UNKNOWN))
    for reason in (" ", "a\x00b", "a" * 2001):
        with pytest.raises(ValueError):
            u.resolve_unknown(target, reason)
    with pytest.raises(AttemptConflict):
        u.resolve_unknown(AttemptState.RETRY_WAIT, "Fictional reason")
    settled = u.resolve_unknown(target, "  Fictional human verification  ")
    assert settled.state == target and settled.resolution_reason == "Fictional human verification"
    assert settled.retry_at is None and u.resolution_reason is None
    with pytest.raises(AttemptConflict):
        settled.retry_explicitly(NOW)
    with pytest.raises(FrozenInstanceError):
        u.resolution_reason = "changed"


def test_bounded_retry_deadlines_and_generations_apply_only_after_explicit_nonacceptance():
    state, at = MailAttempt(ID), NOW
    for gen, delay in enumerate((1, 5, 15, 60, 240), start=1):
        token = UUID(int=gen)
        sending = state.claim(at, token, at + LEASE)
        assert sending.generation == gen and sending.retry_at is None
        state = sending.record(at, gen, token, MailResult(D.TEMPORARY_FAILURE, 451))
        assert state.state == AttemptState.RETRY_WAIT and state.retry_at == at + timedelta(
            minutes=delay
        )
        at = state.retry_at
        with pytest.raises(AttemptConflict):
            state.claim(at - timedelta(microseconds=1), token, at + LEASE)
    pending = state.claim(at, TOKEN, at + LEASE)
    failed = pending.record(at, 6, TOKEN, MailResult(D.TEMPORARY_FAILURE))
    assert failed.state == AttemptState.FAILED and failed.retry_at is None
    with pytest.raises(AttemptConflict):
        failed.claim(at, TOKEN, at + LEASE)
    explicit = failed.retry_explicitly(NOW).claim(at, TOKEN, at + LEASE)
    assert explicit.generation == 7 and explicit.delivery_id == ID and explicit.retries_used == 0
    with pytest.raises(AttemptConflict):
        explicit.record(at, 6, TOKEN, MailResult(D.ACCEPTED))


def test_terminal_acceptance_and_immediate_permanent_failure_have_no_automatic_due_time():
    for d, state in [
        (D.ACCEPTED, AttemptState.ACCEPTED),
        (D.PERMANENT_FAILURE, AttemptState.FAILED),
    ]:
        final = leased().record(NOW, 1, TOKEN, MailResult(d))
        assert final.state == state and final.retry_at is None
        with pytest.raises(AttemptConflict):
            final.claim(NOW, TOKEN, NOW + LEASE)
        if state == AttemptState.FAILED:
            assert final.retry_explicitly(NOW).state == AttemptState.RETRY_WAIT
        else:
            with pytest.raises(AttemptConflict):
                final.retry_explicitly(NOW)


def test_only_explicit_instants_and_positive_intervals_can_form_a_lease():
    for start, end in [(NOW.replace(tzinfo=None), NOW + LEASE), (NOW, NOW), (NOW, NOW - LEASE)]:
        with pytest.raises(ValueError):
            MailAttempt(ID).claim(start, TOKEN, end)
    local = NOW.astimezone(timezone(timedelta(hours=8)))
    active = MailAttempt(ID).claim(local, TOKEN, local + LEASE)
    assert active.lease_until == NOW + LEASE and active.lease_until.tzinfo == UTC
    with pytest.raises(AttemptConflict):
        active.expire(NOW + LEASE - timedelta(microseconds=1))


@pytest.mark.parametrize("missing", [None, False, "fictional-not-a-uuid"])
def test_claim_requires_a_typed_lease_proof(missing):
    pending = MailAttempt(ID)
    with pytest.raises(ValueError, match="UUID lease token"):
        pending.claim(NOW, missing, NOW + LEASE)
    assert pending.state == AttemptState.PENDING and pending.generation == 0


@pytest.mark.parametrize(
    "generation,proof", [(True, TOKEN), (1.0, TOKEN), (1, None), (1, str(TOKEN))]
)
def test_result_requires_complete_typed_evidence_even_when_numbers_compare_equal(generation, proof):
    active = MailAttempt(ID).claim(NOW, TOKEN, NOW + LEASE)
    with pytest.raises(AttemptConflict):
        active.record(NOW, generation, proof, MailResult(D.ACCEPTED))
    assert active.state == AttemptState.SENDING
    assert active.record(NOW, 1, TOKEN, MailResult(D.ACCEPTED)).state == AttemptState.ACCEPTED
