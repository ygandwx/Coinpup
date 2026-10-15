"""Fictional cash refunds, original returns and permanent receipts in the complete bundle."""

import json
from decimal import Decimal
from uuid import uuid4

from check_business_settlement_restore import seed_business_settlements, verify_business_settlements
from coinpup_api.business.allocation_return_schemas import BusinessAllocationReturnCreate
from coinpup_api.business.allocation_returns import BusinessAllocationReturnService
from coinpup_api.business.refund_reads import BusinessRefundReads
from coinpup_api.business.refund_schemas import BusinessRefundCreate
from coinpup_api.business.refunds import BusinessRefundService
from coinpup_api.business.settlement_models import BusinessAllocationLine
from coinpup_api.business.settlement_reads import BusinessSettlementReads
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.models import Entity, JournalLine, Ledger
from coinpup_api.ledger.period_models import LedgerPeriod
from coinpup_api.ledger.period_schemas import PeriodChange
from coinpup_api.ledger.period_service import PeriodService
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.schemas import AccountUpdate, EntityUpdate
from coinpup_api.ledger.service import LedgerService
from sqlalchemy import func, select


def classification(engine, ledger):
    with engine.connect() as c:
        return {
            (asset, role): amount
            for asset, role, amount in c.execute(
                select(JournalLine.asset_id, JournalLine.role, func.sum(JournalLine.amount))
                .where(JournalLine.ledger_id == ledger, JournalLine.role.in_(["income", "expense"]))
                .group_by(JournalLine.asset_id, JournalLine.role)
            )
        }


def seed_refunds(engine, owner):
    original = seed_business_settlements(engine, owner)
    ledger = original["ledger"]
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
    prior_classification = classification(engine, ledger)
    payments = {c["payload"].id: c["payload"] for c in original["cases"] if c["method"] == "pay"}
    # The source fixture has a USD-only bank; explicitly enable actual EUR receipts.
    account_id = next(iter(payments.values())).account_id
    account = next(row for row in structure.list_accounts(owner, ledger) if row.id == account_id)
    structure.update_account(
        owner,
        ledger,
        account_id,
        AccountUpdate(
            expected_version=account.version,
            asset_ids=sorted(set(account.asset_ids) | {"EUR"}),
        ),
    )
    returns, refunds = [], []
    for case in original["cases"]:
        if case["method"] != "allocate":
            continue
        source = case["payload"]
        payment = payments[source.payment_id]
        with engine.connect() as c:
            member = c.scalar(
                select(BusinessAllocationLine.id).where(
                    BusinessAllocationLine.allocation_id == source.id,
                    BusinessAllocationLine.line_no == 1,
                )
            )
        payload = BusinessAllocationReturnCreate(
            id=uuid4(),
            payment_id=payment.id,
            return_date="2026-10-03",
            reason="Fictional explicit release before actual refund",
            lines=[
                dict(
                    source_allocation_line_id=member,
                    expected_version=1,
                    payment_amount=source.lines[0].payment_amount,
                    document_amount=source.lines[0].document_amount,
                )
            ],
        )
        key = "fictional-refund-release-" + str(source.id)
        raw = json.dumps(payload.model_dump(mode="json"), indent=2).encode()
        result = BusinessAllocationReturnService(engine).create(
            owner, ledger, payload, key, raw_body=raw
        )
        returns.append(dict(payload=payload, key=key, raw=raw, result=result))
        incoming = payment.direction == "payout"
        for principal, actual in (
            (("22", "19.37"), ("44", "38.74")) if incoming else (("20", "20"), ("40", "40"))
        ):
            fees = (
                []
                if incoming or refunds
                else [payment.fees[0].model_copy(update={"amount": "0.25"})]
            )
            payload = BusinessRefundCreate(
                id=uuid4(),
                payment_id=payment.id,
                expected_version=1,
                account_id=payment.account_id,
                asset_id="EUR" if incoming else "USD",
                amount=actual,
                payment_amount=principal,
                transaction_date="2026-10-04",
                reason="Fictional actual refund 中文",
                allocation_return_ids=[result.id],
                fees=fees,
            )
            key = "fictional-actual-refund-" + str(len(refunds))
            raw = json.dumps(payload.model_dump(mode="json"), indent=2).encode()
            saved = BusinessRefundService(engine).create(owner, ledger, payload, key, raw_body=raw)
            refunds.append(dict(payload=payload, key=key, raw=raw, result=saved))
    # An unapplied advance needs no artificial allocation-return event.
    advance = next(p for p in payments.values() if p.initial_allocation is None)
    payload = BusinessRefundCreate(
        id=uuid4(),
        payment_id=advance.id,
        expected_version=1,
        account_id=advance.account_id,
        asset_id=advance.asset_id,
        amount=advance.amount,
        payment_amount=advance.amount,
        transaction_date="2026-10-04",
        reason="Fictional full advance refund",
    )
    raw = json.dumps(payload.model_dump(mode="json"), indent=2).encode()
    key = "fictional-advance-refund"
    result = BusinessRefundService(engine).create(owner, ledger, payload, key, raw_body=raw)
    refunds.append(dict(payload=payload, key=key, raw=raw, result=result))
    expected_classification = prior_classification | {
        ("USD", "expense"): prior_classification[("USD", "expense")] + Decimal("0.25")
    }
    if classification(engine, ledger) != expected_classification:
        raise RuntimeError("Refund principal changed classification or duplicated fees")
    PeriodService(engine).change(
        owner,
        ledger,
        PeriodChange(
            action="close",
            expected_version=period_version,
            closed_through="2026-10-04",
            reason="Fictional refunded settlement recovery",
        ),
        "fictional-refund-close",
    )
    structure.update_entity(
        owner, entity, EntityUpdate(expected_version=opened.version, archived=True)
    )
    # Refresh current reads after new facts; keep all original request/receipt pairs exact.
    reads = BusinessSettlementReads(engine)
    original["readings"] = {
        method: [getattr(reads, method)(owner, ledger, row.id) for row in saved]
        for method, saved in original["readings"].items()
    }
    original["money"] = PostingService(engine).balances(owner, ledger)
    original["controls"] = ControlAccounts(engine).balances(owner, ledger)
    reader = BusinessRefundReads(engine)
    capacities = [reader.capacity(owner, ledger, identifier) for identifier in payments]
    if any(row.refundable_amount != "0.00" for row in capacities):
        raise RuntimeError("Fictional refund cases did not exhaust released principal")
    return dict(
        owner=owner,
        ledger=ledger,
        original=original,
        returns=returns,
        cases=refunds,
        summaries=reader.list_refunds(owner, ledger),
        capacities=capacities,
        classification=expected_classification,
        details=[reader.get_refund(owner, ledger, c["payload"].id) for c in refunds],
    )


def verify_refunds(engine, evidence):
    owner, ledger = evidence["owner"], evidence["ledger"]
    verify_business_settlements(engine, evidence["original"])
    reads = BusinessRefundReads(engine)
    if (
        reads.list_refunds(owner, ledger) != evidence["summaries"]
        or classification(engine, ledger) != evidence["classification"]
    ):
        raise RuntimeError("Restored refund history or independent classification changed")
    for method, rows in (("get_refund", evidence["details"]), ("capacity", evidence["capacities"])):
        if [getattr(reads, method)(owner, ledger, row.id) for row in rows] != rows:
            raise RuntimeError("Restored actual refunds or remaining principal changed")
    for service, cases in (
        (BusinessAllocationReturnService(engine), evidence["returns"]),
        (BusinessRefundService(engine), evidence["cases"]),
    ):
        for case in cases:
            if (
                service.create(owner, ledger, case["payload"], case["key"], raw_body=case["raw"])
                != case["result"]
            ):
                raise RuntimeError("Restored original refund or return receipt changed")
