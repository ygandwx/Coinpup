"""Fictional settled and unapplied payments preserved by the full database/file bundle."""

import json
from uuid import uuid4

from coinpup_api.business.confirmation_schemas import BusinessConfirmationCreate
from coinpup_api.business.confirmations import BusinessConfirmationService
from coinpup_api.business.draft_schemas import BusinessDraftCreate
from coinpup_api.business.drafts import DraftService
from coinpup_api.business.schemas import PartyCreate, PartyUpdate
from coinpup_api.business.service import BusinessService
from coinpup_api.business.settlement_schemas import BusinessAllocationCreate, BusinessPaymentCreate
from coinpup_api.business.settlements import BusinessSettlementService
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.period_schemas import PeriodChange
from coinpup_api.ledger.period_service import PeriodService
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.posting_schemas import OpeningCreate
from coinpup_api.ledger.schemas import AccountCreate, AccountUpdate, EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerService


def seed_business_settlements(engine, owner):
    structure = LedgerService(engine)
    entity = structure.create_entity(
        owner,
        EntityCreate(
            kind="personal",
            name="Fictional 结算恢复",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    ledger = entity.ledger.id
    bank = structure.create_account(
        owner,
        ledger,
        AccountCreate(name="Fictional settlement bank", kind="bank", asset_ids=["USD"]),
    )
    master = BusinessService(engine)
    party = master.create_party(
        owner, ledger, PartyCreate(id=uuid4(), name="Fictional 往来恢复", role="both")
    )
    categories = {row.kind: row.id for row in structure.list_categories(owner, ledger)}
    opening = OpeningCreate(
        account_id=bank.id,
        asset_id="USD",
        amount="1000.00",
        transaction_date="2026-09-01",
        description="Fictional opening recovery",
    )
    opening_result = PostingService(engine).post_opening(
        owner, ledger, opening, "fictional-opening"
    )
    service = BusinessSettlementService(engine)
    cases = []
    for kind, direction, asset, tax, part, rest in (
        ("invoice", "receipt", "USD", "10", ("50", "50"), ("60", "60")),
        ("bill", "payout", "EUR", "0", ("44", "40"), ("66", "60")),
    ):
        document = DraftService(engine).create_draft(
            owner,
            ledger,
            BusinessDraftCreate(
                id=uuid4(),
                document_kind=kind,
                party_id=party.id,
                asset_id=asset,
                issue_date="2026-09-30",
                lines=[
                    dict(
                        id=uuid4(),
                        description="Fictional settlement source",
                        quantity="1",
                        unit_price="100",
                        tax_rate_percent=tax,
                        recognition_date="2026-09-30",
                        category_id=categories["income" if kind == "invoice" else "expense"],
                    )
                ],
            ),
        )
        BusinessConfirmationService(engine).confirm(
            owner,
            ledger,
            document.id,
            BusinessConfirmationCreate(id=uuid4(), expected_version=document.version),
            "fictional-confirm-" + kind,
        )

        def group(amounts, document=document):
            return dict(
                id=uuid4(),
                allocation_date="2026-10-02",
                lines=[
                    dict(
                        document_id=document.id,
                        document_line_id=document.lines[0].id,
                        payment_amount=amounts[0],
                        document_amount=amounts[1],
                    )
                ],
            )

        payment = BusinessPaymentCreate(
            id=uuid4(),
            direction=direction,
            party_id=party.id,
            account_id=bank.id,
            asset_id="USD",
            amount="110.00",
            transaction_date="2026-10-01",
            description="Fictional settlement recovery",
            initial_allocation=group(part),
            fees=[
                dict(
                    account_id=bank.id,
                    asset_id="USD",
                    amount="1",
                    category_id=categories["expense"],
                )
            ],
        )
        allocation = BusinessAllocationCreate(payment_id=payment.id, **group(rest))
        for method, payload in (("pay", payment), ("allocate", allocation)):
            key = "fictional-" + kind + "-" + method
            raw = json.dumps(payload.model_dump(mode="json"), indent=2).encode()
            result = getattr(service, method)(owner, ledger, payload, key, raw_body=raw)
            cases.append(dict(method=method, payload=payload, key=key, raw=raw, result=result))
    advance = BusinessPaymentCreate(
        id=uuid4(),
        direction="payout",
        party_id=party.id,
        account_id=bank.id,
        asset_id="USD",
        amount="10",
        transaction_date="2026-10-01",
        description="Fictional unapplied advance",
    )
    raw = json.dumps(advance.model_dump(mode="json")).encode()
    result = service.pay(owner, ledger, advance, "fictional-unapplied", raw_body=raw)
    cases.append(
        dict(method="pay", payload=advance, key="fictional-unapplied", raw=raw, result=result)
    )
    PeriodService(engine).change(
        owner,
        ledger,
        PeriodChange(
            action="close",
            expected_version=1,
            closed_through="2026-10-02",
            reason="Fictional settlement recovery boundary",
        ),
        "fictional-close",
    )
    master.update_party(owner, ledger, party.id, PartyUpdate(expected_version=1, archived=True))
    structure.update_account(
        owner, ledger, bank.id, AccountUpdate(expected_version=1, archived=True)
    )
    structure.update_entity(owner, entity.id, EntityUpdate(expected_version=1, archived=True))
    money = PostingService(engine).balances(owner, ledger)
    controls = ControlAccounts(engine).balances(owner, ledger)
    if len(money) != 1 or money[0].amount != "988.00":
        raise RuntimeError("Fictional settlement cash and independent fees are inconsistent")
    if [(row.system_key, row.asset_id, row.amount) for row in controls if row.amount != "0.00"] != [
        ("advance.paid", "USD", "10.00")
    ]:
        raise RuntimeError("Fictional settlement sources or unapplied advance are inconsistent")
    return dict(
        owner=owner,
        ledger=ledger,
        cases=cases,
        money=money,
        controls=controls,
        opening=opening,
        opening_result=opening_result,
    )


def verify_business_settlements(engine, evidence):
    owner, ledger = evidence["owner"], evidence["ledger"]
    if (
        PostingService(engine).balances(owner, ledger) != evidence["money"]
        or ControlAccounts(engine).balances(owner, ledger) != evidence["controls"]
    ):
        raise RuntimeError("Restored fictional settlement balances changed")
    if (
        PostingService(engine).post_opening(owner, ledger, evidence["opening"], "fictional-opening")
        != evidence["opening_result"]
    ):
        raise RuntimeError("Restored fictional original opening receipt changed")
    service = BusinessSettlementService(engine)
    for case in evidence["cases"]:
        if (
            getattr(service, case["method"])(
                owner, ledger, case["payload"], case["key"], raw_body=case["raw"]
            )
            != case["result"]
        ):
            raise RuntimeError("Restored fictional settlement receipt changed")
