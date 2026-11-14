"""Fictional refund version chains, frozen requests and linked private evidence in a backup."""

import json
from decimal import Decimal

from check_refund_restore import classification, seed_refunds
from coinpup_api.business.allocation_returns import BusinessAllocationReturnService
from coinpup_api.business.refund_reads import BusinessRefundReads
from coinpup_api.business.refund_revision_history import BusinessRefundHistory
from coinpup_api.business.refund_revision_schemas import (
    RefundCancellationCreate,
    RefundCorrectionCreate,
)
from coinpup_api.business.refund_revisions import BusinessRefundRevisionService
from coinpup_api.business.refunds import BusinessRefundService
from coinpup_api.files.service import DocumentService
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.models import Entity, Ledger
from coinpup_api.ledger.period_models import LedgerPeriod
from coinpup_api.ledger.period_schemas import PeriodChange
from coinpup_api.ledger.period_service import PeriodService
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.schemas import AccountUpdate, EntityUpdate
from coinpup_api.ledger.service import LedgerService
from sqlalchemy import select


def current_state(engine, owner, ledger, cases):
    reader = BusinessRefundReads(engine)
    history = BusinessRefundHistory(engine)
    return dict(
        current=[reader.current(owner, ledger, c["payload"].id) for c in cases],
        history=[history.list_history(owner, ledger, c["payload"].id) for c in cases],
        detail=[reader.get_refund(owner, ledger, c["payload"].id) for c in cases],
        capacities=[reader.capacity(owner, ledger, c["payload"].payment_id) for c in cases],
        classification=classification(engine, ledger),
        controls=ControlAccounts(engine).balances(owner, ledger),
        balances=PostingService(engine).balances(owner, ledger),
    )


def seed_refund_revisions(engine, owner, files, store, pdf, upload):
    # Preserve the original-refund exercise on its own ledger.
    source = seed_refunds(engine, owner)
    ledger = source["ledger"]
    same = next(c for c in source["cases"] if c["payload"].fees)
    cross = next(c for c in source["cases"] if c["payload"].asset_id == "EUR")
    cases = [same, cross]
    with engine.connect() as c:
        entity, version = c.execute(
            select(Entity.id, Entity.version)
            .join(Ledger, Ledger.entity_id == Entity.id)
            .where(Ledger.id == ledger)
        ).one()
        period_version = c.scalar(
            select(LedgerPeriod.version).where(LedgerPeriod.ledger_id == ledger)
        )
    structure = LedgerService(engine)
    opened = structure.update_entity(
        owner, entity, EntityUpdate(expected_version=version, archived=False)
    )
    bank = next(
        item
        for item in structure.list_accounts(owner, ledger, include_archived=True)
        if item.id == same["payload"].account_id
    )
    account = structure.update_account(
        owner, ledger, bank.id, AccountUpdate(expected_version=bank.version, archived=False)
    )
    periods = PeriodService(engine)
    period = periods.change(
        owner,
        ledger,
        PeriodChange(
            action="reopen",
            expected_version=period_version,
            closed_through=None,
            reason="Fictional explicit reopen for refund corrections",
        ),
        "fictional-reopen-revisions",
    )
    before = current_state(engine, owner, ledger, cases)
    service = BusinessRefundRevisionService(engine)
    revisions = []
    originals = []
    legs = [
        (same, 1, "correct", "4.00", "4.00", False),
        (same, 2, "correct", "2.50", "2.50", True),
        (same, 3, "cancel", None, None, False),
        (cross, 1, "correct", "9.37", "11.00", True),
    ]
    for index, (case, version, action, cash, principal, keep_return) in enumerate(legs):
        original = case["payload"]
        body = dict(expected_version=version, reason=f"Fictional preserved revision {index} 中文")
        if action == "correct":
            fees = (
                [
                    same["payload"].fees[0].model_copy(update={"amount": q}).model_dump(mode="json")
                    for q in ("0.03", "0.02")
                ]
                if case is cross
                else []
            )
            body["replacement"] = dict(
                account_id=str(original.account_id),
                asset_id=original.asset_id,
                amount=cash,
                payment_amount=principal,
                transaction_date="2026-10-05",
                reason="Fictional revised cash",
                allocation_return_ids=[str(i) for i in original.allocation_return_ids]
                if keep_return
                else [],
                fees=fees,
            )
        command = (
            RefundCorrectionCreate if action == "correct" else RefundCancellationCreate
        ).model_validate(body)
        key = f"fictional-restored-refund-revision-{index}"
        raw = json.dumps(body, ensure_ascii=False, indent=2).encode()
        saved = getattr(service, action)(owner, ledger, original.id, command, key, raw_body=raw)
        revisions.append(
            dict(id=original.id, action=action, payload=command, key=key, raw=raw, result=saved)
        )
        if index == 0:
            originals.append(
                upload(
                    files,
                    store,
                    owner,
                    ledger,
                    pdf,
                    "Fictional corrected refund evidence.pdf",
                    operation=saved.operation_id,
                )
            )
    periods.change(
        owner,
        ledger,
        PeriodChange(
            action="close",
            expected_version=period.version,
            closed_through="2026-10-05",
            reason="Fictional close after refund revisions",
        ),
        "fictional-close-revisions",
    )
    structure.update_account(
        owner, ledger, bank.id, AccountUpdate(expected_version=account.version, archived=True)
    )
    structure.update_entity(
        owner, entity, EntityUpdate(expected_version=opened.version, archived=True)
    )
    after = current_state(engine, owner, ledger, cases)
    for case, old, new, revised in zip(
        cases, before["capacities"], after["capacities"], ("0", "11"), strict=True
    ):
        released = Decimal(case["payload"].payment_amount) - Decimal(revised)
        if (
            old.applied_amount != new.applied_amount
            or Decimal(new.refundable_amount) != Decimal(old.refundable_amount) + released
        ):
            raise RuntimeError(
                "Revised actual cash did not release only its original payment principal"
            )
    totals = before["classification"].copy()
    totals[("USD", "expense")] += Decimal("0.05") - Decimal(same["payload"].fees[0].amount)
    if after["classification"] != totals or [r.status for r in after["current"]] != [
        "cancelled",
        "active",
    ]:
        raise RuntimeError("Restored scenario changed operating classification or terminal status")
    return dict(
        owner=owner,
        ledger=ledger,
        original_cases=source["cases"],
        returns=source["returns"],
        cases=cases,
        revisions=revisions,
        expected=after,
        originals=originals,
        links=files.list_operation_files(owner, ledger, same["result"].operation_id),
    )


def verify_refund_revisions(engine, evidence):
    owner, ledger = evidence["owner"], evidence["ledger"]
    cases = evidence["cases"]
    if current_state(engine, owner, ledger, cases) != evidence["expected"]:
        raise RuntimeError("Restored refund facts, history, balances or capacity differ")
    history = BusinessRefundHistory(engine)
    revisions = BusinessRefundRevisionService(engine)
    for case in evidence["revisions"]:
        read = history.get_revision(owner, ledger, case["id"], case["result"].revision_id)
        replay = getattr(revisions, case["action"])(
            owner, ledger, case["id"], case["payload"], case["key"], raw_body=case["raw"]
        )
        if read != case["result"] or replay != read:
            raise RuntimeError("A fixed refund revision changed after restoring its private bundle")
    for service, items in (
        (BusinessRefundService(engine), evidence["original_cases"]),
        (BusinessAllocationReturnService(engine), evidence["returns"]),
    ):
        for case in items:
            if (
                service.create(owner, ledger, case["payload"], case["key"], raw_body=case["raw"])
                != case["result"]
            ):
                raise RuntimeError("A restored original refund or allocation return lost its 201")
    files = DocumentService(engine)
    if (
        files.list_operation_files(owner, ledger, cases[0]["result"].operation_id)
        != evidence["links"]
    ):
        raise RuntimeError("The terminal refund lost its linked private original")
