"""Fictional confirmed invoices/bills and exact v2 replay after full bundle recovery."""

import json
from uuid import uuid4

from coinpup_api.business.confirmation_models import BusinessConfirmation
from coinpup_api.business.confirmation_schemas import BusinessConfirmationCreate
from coinpup_api.business.confirmations import BusinessConfirmationService
from coinpup_api.business.draft_schemas import BusinessDraftCreate
from coinpup_api.business.drafts import DraftService
from coinpup_api.business.schemas import PartyCreate
from coinpup_api.business.service import BusinessService
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.schemas import EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerService
from sqlalchemy import select


def seed_business_confirmations(engine, owner):
    structure = LedgerService(engine)
    entity = structure.create_entity(
        owner,
        EntityCreate(
            kind="personal",
            name="Fictional 确认恢复",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    ledger = entity.ledger.id
    party = BusinessService(engine).create_party(
        owner,
        ledger,
        PartyCreate(
            id=uuid4(),
            name="Fictional 往来",
            role="both",
        ),
    )
    categories = {row.kind: row.id for row in structure.list_categories(owner, ledger)}
    cases = []
    for kind, category in (("invoice", "income"), ("bill", "expense")):
        document = DraftService(engine).create_draft(
            owner,
            ledger,
            BusinessDraftCreate(
                id=uuid4(),
                document_kind=kind,
                party_id=party.id,
                asset_id="USD",
                issue_date="2026-10-09",
                lines=[
                    dict(
                        id=uuid4(),
                        description="Fictional 服务",
                        quantity="1",
                        unit_price="100.00",
                        tax_rate_percent="10",
                        category_id=categories[category],
                        recognition_date="2026-09-30",
                    ),
                    dict(
                        id=uuid4(),
                        description="Fictional 零行",
                        quantity="1",
                        unit_price="0",
                        category_id=categories[category],
                        recognition_date="2026-08-31",
                    ),
                ],
            ),
        )
        payload = BusinessConfirmationCreate(id=uuid4(), expected_version=document.version)
        key = "fictional-confirmation-restore-" + kind
        raw = json.dumps(payload.model_dump(mode="json")).encode()
        result = BusinessConfirmationService(engine).confirm(
            owner,
            ledger,
            document.id,
            payload,
            key,
            raw_body=raw,
        )
        with engine.connect() as c:
            proof = c.scalar(
                select(BusinessConfirmation.snapshot).where(
                    BusinessConfirmation.id == payload.id,
                )
            )
        cases.append(
            dict(
                document=document.id, payload=payload, key=key, raw=raw, result=result, proof=proof
            )
        )
    structure.update_entity(owner, entity.id, EntityUpdate(expected_version=1, archived=True))
    balances = ControlAccounts(engine).balances(owner, ledger)
    return dict(owner=owner, ledger=ledger, cases=cases, balances=balances)


def verify_business_confirmations(engine, evidence):
    owner, ledger = evidence["owner"], evidence["ledger"]
    if ControlAccounts(engine).balances(owner, ledger) != evidence["balances"]:
        raise RuntimeError("Restored fictional business control balances changed")
    for case in evidence["cases"]:
        result = BusinessConfirmationService(engine).confirm(
            owner,
            ledger,
            case["document"],
            case["payload"],
            case["key"],
            raw_body=case["raw"],
        )
        with engine.connect() as c:
            proof = c.scalar(
                select(BusinessConfirmation.snapshot).where(
                    BusinessConfirmation.id == case["payload"].id,
                )
            )
        if result != case["result"] or proof != case["proof"]:
            raise RuntimeError("Restored fictional business confirmation or v2 receipt changed")
