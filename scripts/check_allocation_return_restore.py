"""Fictional return history and original receipts survive the full private-file bundle."""

import json
from uuid import uuid4

from check_business_settlement_restore import seed_business_settlements, verify_business_settlements
from coinpup_api.business.allocation_return_reads import BusinessAllocationReturnReads
from coinpup_api.business.allocation_return_schemas import BusinessAllocationReturnCreate
from coinpup_api.business.allocation_returns import BusinessAllocationReturnService
from coinpup_api.business.settlement_reads import BusinessSettlementReads
from coinpup_api.business.settlement_schemas import BusinessAllocationCreate
from coinpup_api.business.settlements import BusinessSettlementService
from coinpup_api.ledger.models import Entity, Ledger
from coinpup_api.ledger.period_models import LedgerPeriod
from coinpup_api.ledger.period_schemas import PeriodChange
from coinpup_api.ledger.period_service import PeriodService
from coinpup_api.ledger.schemas import EntityUpdate
from coinpup_api.ledger.service import LedgerService
from sqlalchemy import select


def seed_allocation_returns(engine, owner):
    # Reuse proven fictional settlements, in a separate ledger from their original fixture.
    original = seed_business_settlements(engine, owner)
    ledger = original["ledger"]
    with engine.connect() as connection:
        entity_id, version = connection.execute(
            select(Entity.id, Entity.version)
            .join(Ledger, Ledger.entity_id == Entity.id)
            .where(Ledger.id == ledger)
        ).one()
        period_version = connection.scalar(
            select(LedgerPeriod.version).where(LedgerPeriod.ledger_id == ledger)
        )
    structure = LedgerService(engine)
    updated = structure.update_entity(
        owner, entity_id, EntityUpdate(expected_version=version, archived=False)
    )
    settlements = BusinessSettlementService(engine)
    service, reads = BusinessAllocationReturnService(engine), BusinessAllocationReturnReads(engine)
    cases, reapplied, capacities = [], [], []
    for index, case in enumerate(original["cases"]):
        if case["method"] != "allocate":
            continue
        source = case["payload"]
        capacity = reads.capacity(owner, ledger, source.id)
        line = capacity.lines[0]
        cross = capacity.asset_id != line.document_asset_id
        # Partial then final return preserves the original rate and exhausts this mapping.
        for part, amounts in enumerate(
            (("22" if cross else "20", "20"), ("44" if cross else "40", "40"))
        ):
            payload = BusinessAllocationReturnCreate(
                id=uuid4(),
                payment_id=source.payment_id,
                return_date="2026-10-03",
                reason="Fictional bundle return 中文",
                lines=[
                    dict(
                        source_allocation_line_id=line.source_allocation_line_id,
                        expected_version=line.expected_version,
                        payment_amount=amounts[0],
                        document_amount=amounts[1],
                    )
                ],
            )
            key = f"fictional-return-{index}-{part}"
            raw = json.dumps(payload.model_dump(mode="json"), indent=2).encode()
            result = service.create(owner, ledger, payload, key, raw_body=raw)
            cases.append(dict(payload=payload, key=key, raw=raw, result=result))
        exhausted = reads.capacity(owner, ledger, source.id)
        if any(
            item.remaining_payment_amount != "0.00" or item.remaining_document_amount != "0.00"
            for item in exhausted.lines
        ):
            raise RuntimeError("Fictional original allocation did not fully return")
        capacities.append(exhausted)
        # Reuse the exact released principal, while the original receipt remains unchanged.
        payload = BusinessAllocationCreate.model_validate(
            source.model_dump(mode="json") | {"id": str(uuid4()), "allocation_date": "2026-10-03"}
        )
        key = f"fictional-reapply-{index}"
        raw = json.dumps(payload.model_dump(mode="json"), indent=2).encode()
        result = settlements.allocate(owner, ledger, payload, key, raw_body=raw)
        reapplied.append(dict(payload=payload, key=key, raw=raw, result=result))
        capacities.append(reads.capacity(owner, ledger, payload.id))
    if len(cases) != 4 or len(reapplied) != 2:
        raise RuntimeError("Fictional same/cross-asset return cases are incomplete")
    PeriodService(engine).change(
        owner,
        ledger,
        PeriodChange(
            action="close",
            expected_version=period_version,
            closed_through="2026-10-03",
            reason="Fictional returned settlement recovery",
        ),
        "fictional-return-close",
    )
    structure.update_entity(
        owner, entity_id, EntityUpdate(expected_version=updated.version, archived=True)
    )
    verify_business_settlements(engine, original)
    return dict(
        original=original,
        owner=owner,
        ledger=ledger,
        cases=cases,
        reapplied=reapplied,
        capacities=capacities,
        summaries=reads.list_returns(owner, ledger),
        details=[reads.get_return(owner, ledger, c["payload"].id) for c in cases],
        reapplied_details=[
            BusinessSettlementReads(engine).get_allocation(owner, ledger, c["payload"].id)
            for c in reapplied
        ],
    )


def verify_allocation_returns(engine, evidence):
    owner, ledger = evidence["owner"], evidence["ledger"]
    verify_business_settlements(engine, evidence["original"])
    reads = BusinessAllocationReturnReads(engine)
    if reads.list_returns(owner, ledger) != evidence["summaries"]:
        raise RuntimeError("Restored return list changed")
    for row in evidence["details"]:
        if reads.get_return(owner, ledger, row.id) != row:
            raise RuntimeError("Restored original return detail changed")
    for row in evidence["capacities"]:
        if reads.capacity(owner, ledger, row.id) != row:
            raise RuntimeError("Restored per-source return capacity changed")
    for row in evidence["reapplied_details"]:
        if BusinessSettlementReads(engine).get_allocation(owner, ledger, row.id) != row:
            raise RuntimeError("Restored replacement allocation changed")
    for service, method, cases in (
        (BusinessAllocationReturnService(engine), "create", evidence["cases"]),
        (BusinessSettlementService(engine), "allocate", evidence["reapplied"]),
    ):
        for case in cases:
            if (
                getattr(service, method)(
                    owner, ledger, case["payload"], case["key"], raw_body=case["raw"]
                )
                != case["result"]
            ):
                raise RuntimeError("Restored original return intent receipt changed")
