"""Fictional period histories for the guarded disposable bundle recovery check."""

import json

from coinpup_api.ledger.period_reads import PeriodReads
from coinpup_api.ledger.period_schemas import PeriodChange
from coinpup_api.ledger.period_service import PeriodService
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.posting_schemas import CancellationCreate, OpeningCreate
from coinpup_api.ledger.schemas import AccountCreate, EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError, LedgerService


def seed_periods(engine, owner):
    structure, posting = LedgerService(engine), PostingService(engine)
    periods, reads = PeriodService(engine), PeriodReads(engine)
    entity = structure.create_entity(
        owner, EntityCreate(kind="personal", name="Fictional period recovery", base_asset_id="USD")
    )
    ledger = entity.ledger.id
    account = structure.create_account(
        owner, ledger, AccountCreate(name="Fictional period cash", kind="cash", asset_ids=["USD"])
    )
    opening = OpeningCreate(
        account_id=account.id,
        asset_id="USD",
        amount="100.00",
        transaction_date="2026-01-20",
        description="Fictional closed period original",
    )
    original = posting.post_opening(owner, ledger, opening, "fictional-period-opening")
    plans = []
    cancellation = CancellationCreate(expected_version=1, reason="Fictional reopened cancellation")
    for version, action, cutoff in [
        (1, "close", "2026-01-31"),
        (2, "reopen", "2026-01-15"),
        (3, "close", "2026-01-31"),
    ]:
        body = dict(
            expected_version=version,
            action=action,
            closed_through=cutoff,
            reason=" Fictional period recovery reason ",
        )
        command = PeriodChange.model_validate(body)
        raw, key = json.dumps(body, indent=2).encode(), f"fictional-period-{version}"
        receipt = periods.change(owner, ledger, command, key, raw_body=raw)
        plans.append((command, key, raw, receipt))
        if action == "reopen":
            cancelled = posting.cancel_operation(
                owner, ledger, original.id, cancellation, "fictional-period-cancel"
            )
    structure.update_entity(owner, entity.id, EntityUpdate(expected_version=1, archived=True))
    return dict(
        ledger=ledger,
        plans=plans,
        original=original,
        opening=opening,
        cancellation=cancellation,
        cancelled=cancelled,
        state=reads.state(owner, ledger).model_dump(mode="json", exclude={"generated_at"}),
        history=reads.history(owner, ledger),
    )


def verify_periods(engine, owner, fixture):
    ledger = fixture["ledger"]
    periods, reads, posting = PeriodService(engine), PeriodReads(engine), PostingService(engine)
    assert (
        reads.state(owner, ledger).model_dump(mode="json", exclude={"generated_at"})
        == fixture["state"]
    )
    assert reads.history(owner, ledger) == fixture["history"]
    for command, key, raw, receipt in fixture["plans"]:
        assert periods.change(owner, ledger, command, key, raw_body=raw) == receipt
    command, key, _, _ = fixture["plans"][0]
    try:
        periods.change(owner, ledger, command, key)
    except LedgerError as error:
        assert error.code == "idempotency_conflict"
    else:
        raise AssertionError("Changed raw reason spelling replayed a different intent")
    assert (
        posting.post_opening(owner, ledger, fixture["opening"], "fictional-period-opening")
        == fixture["original"]
    )
    assert (
        posting.cancel_operation(
            owner,
            ledger,
            fixture["original"].id,
            fixture["cancellation"],
            "fictional-period-cancel",
        )
        == fixture["cancelled"]
    )
