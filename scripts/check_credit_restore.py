"""Fictional manual credits after real refunds, preserved by the complete bundle."""

import json
from uuid import uuid4

from check_refund_restore import classification, seed_refunds, verify_refunds
from coinpup_api.business.confirmation_schemas import (
    BusinessConfirmationCreate,
    BusinessConfirmationResponse,
)
from coinpup_api.business.confirmations import BusinessConfirmationService
from coinpup_api.business.credit_reads import BusinessCreditReads
from coinpup_api.business.credit_schemas import BusinessCreditCreate
from coinpup_api.business.credits import BusinessCreditService
from coinpup_api.business.settlement_reads import BusinessSettlementReads
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.models import AssetRecord, CommandReceipt, Entity, Ledger
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.period_models import LedgerPeriod
from coinpup_api.ledger.period_schemas import PeriodChange
from coinpup_api.ledger.period_service import PeriodService
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.schemas import EntityUpdate
from coinpup_api.ledger.service import LedgerService
from sqlalchemy import select


def seed_credits(engine, owner):
    refunds = seed_refunds(engine, owner)
    original = refunds["original"]
    ledger = refunds["ledger"]
    with engine.connect() as connection:
        entity, version = connection.execute(
            select(Entity.id, Entity.version)
            .join(Ledger, Ledger.entity_id == Entity.id)
            .where(Ledger.id == ledger)
        ).one()
        period_version = connection.scalar(
            select(LedgerPeriod.version).where(LedgerPeriod.ledger_id == ledger)
        )
        assets = {
            row.asset_id: asset_definition(row) for row in connection.execute(select(AssetRecord))
        }
        confirmations = [
            (
                key,
                BusinessConfirmationResponse.model_validate(response),
            )
            for key, response in connection.execute(
                select(CommandReceipt.key, CommandReceipt.response).where(
                    CommandReceipt.ledger_id == ledger,
                    CommandReceipt.key.in_(["fictional-confirm-invoice", "fictional-confirm-bill"]),
                )
            )
        ]
    if len(confirmations) != 2:
        raise RuntimeError("Fictional credit sources are incomplete")
    structure = LedgerService(engine)
    opened = structure.update_entity(
        owner, entity, EntityUpdate(expected_version=version, archived=False)
    )
    money = PostingService(engine).balances(owner, ledger)
    expected_classification = classification(engine, ledger)
    reader, service = BusinessCreditReads(engine), BusinessCreditService(engine)
    cases = []
    for _, confirmation in confirmations:
        capacity = reader.capacity(owner, ledger, confirmation.document_id)
        source = capacity.lines[0]
        # Both source documents have exactly 60 of explicitly released gross capacity.
        # Invoice manual net/tax is 17.23+2.77 then 32.77+7.23; the bill is tax-free.
        components = (
            [("17.23", "2.77"), ("32.77", "7.23")]
            if capacity.document_kind == "invoice"
            else [("20", "0"), ("40", "0")]
        )
        if source.available_gross_amount != "60.00":
            raise RuntimeError("Fictional source release is not the expected exact quantity")
        for net, tax in components:
            request = BusinessCreditCreate(
                id=uuid4(),
                document_id=capacity.id,
                expected_version=capacity.expected_version,
                credit_date="2026-10-05",
                reason="Fictional manual credit 中文",
                lines=[
                    dict(
                        source_line_id=source.source_line_id,
                        net_amount=net,
                        tax_amount=tax,
                        recognition_date="2026-10-05",
                    )
                ],
            )
            key = "fictional-manual-credit-" + str(len(cases))
            raw = json.dumps(request.model_dump(mode="json"), indent=2).encode()
            response = service.create(owner, ledger, request, key, raw_body=raw)
            cases.append(dict(payload=request, key=key, raw=raw, result=response))
        role = "income" if capacity.document_kind == "invoice" else "expense"
        asset = assets[capacity.asset_id]
        before = Amount.from_decimal(expected_classification[(asset.asset_id, role)], asset)
        change = Amount.parse("60", asset)
        expected_classification[(asset.asset_id, role)] = (
            before + change if role == "income" else before - change
        ).to_decimal()
        final = reader.capacity(owner, ledger, capacity.id).lines[0]
        if final.available_gross_amount != "0.00":
            raise RuntimeError("Fictional final credit did not consume the explicit release")
    if (
        PostingService(engine).balances(owner, ledger) != money
        or classification(engine, ledger) != expected_classification
    ):
        raise RuntimeError("Credit changed actual cash or did not reduce original classification")
    PeriodService(engine).change(
        owner,
        ledger,
        PeriodChange(
            action="close",
            expected_version=period_version,
            closed_through="2026-10-05",
            reason="Fictional credit recovery boundary",
        ),
        "fictional-credit-close",
    )
    structure.update_entity(
        owner, entity, EntityUpdate(expected_version=opened.version, archived=True)
    )
    # Only current observations change; all old request/receipt pairs stay untouched.
    settlements = BusinessSettlementReads(engine)
    original["readings"] = {
        method: [getattr(settlements, method)(owner, ledger, row.id) for row in saved]
        for method, saved in original["readings"].items()
    }
    original["controls"] = ControlAccounts(engine).balances(owner, ledger)
    refunds["classification"] = expected_classification
    return dict(
        owner=owner,
        ledger=ledger,
        refunds=refunds,
        confirmations=confirmations,
        cases=cases,
        summaries=reader.list_credits(owner, ledger),
        details=[reader.get_credit(owner, ledger, case["payload"].id) for case in cases],
        capacities=[
            reader.capacity(owner, ledger, result.document_id) for _, result in confirmations
        ],
    )


def verify_credits(engine, evidence):
    owner, ledger = evidence["owner"], evidence["ledger"]
    verify_refunds(engine, evidence["refunds"])
    reads = BusinessCreditReads(engine)
    if reads.list_credits(owner, ledger) != evidence["summaries"]:
        raise RuntimeError("Restored manual credit history changed")
    for method, saved in (
        ("get_credit", evidence["details"]),
        ("capacity", evidence["capacities"]),
    ):
        if [getattr(reads, method)(owner, ledger, row.id) for row in saved] != saved:
            raise RuntimeError("Restored credit receipt or source capacity changed")
    for case in evidence["cases"]:
        if (
            BusinessCreditService(engine).create(
                owner, ledger, case["payload"], case["key"], raw_body=case["raw"]
            )
            != case["result"]
        ):
            raise RuntimeError("Restored original manual credit receipt changed")
    for key, original in evidence["confirmations"]:
        response = BusinessConfirmationService(engine).confirm(
            owner,
            ledger,
            original.document_id,
            BusinessConfirmationCreate(id=original.id, expected_version=original.source_version),
            key,
        )
        if response != original:
            raise RuntimeError("Credit altered the original confirmation receipt")
