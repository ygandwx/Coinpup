"""Fictional 100 USD bank evidence and zero-write replay, not an OCR quality sample."""

import json
from uuid import uuid4

from coinpup_api.business.confirmation_schemas import BusinessConfirmationCreate
from coinpup_api.business.confirmations import BusinessConfirmationService
from coinpup_api.business.draft_schemas import BusinessDraftCreate
from coinpup_api.business.drafts import DraftService
from coinpup_api.business.schemas import PartyCreate, PartyUpdate
from coinpup_api.business.service import BusinessService
from coinpup_api.business.settlement_reads import BusinessSettlementReads
from coinpup_api.business.settlement_schemas import BusinessPaymentCreate
from coinpup_api.business.settlements import BusinessSettlementService
from coinpup_api.files.schemas import FileUpdate
from coinpup_api.ledger.assets import fiat_asset
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.period_schemas import PeriodChange
from coinpup_api.ledger.period_service import PeriodService
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.posting_schemas import OpeningCreate
from coinpup_api.ledger.schemas import AccountCreate, AccountUpdate, EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError, LedgerService
from coinpup_api.models import Base
from coinpup_api.ocr.confirmation import ConfirmationService
from coinpup_api.ocr.confirmation_contracts import ConfirmationCreate
from coinpup_api.ocr.confirmation_reads import ConfirmationReadService
from coinpup_api.ocr.contracts import Candidate, Completion, JobCreate
from coinpup_api.ocr.queue import OcrQueueService
from coinpup_api.ocr.review import DraftReviewService
from sqlalchemy import text

ALLOWED = {"ocr_confirmations", "ocr_drafts", "operation_file_links", "change_log"}


def expense(engine, ledger):
    with engine.connect() as connection:
        total = connection.scalar(
            text(
                "SELECT sum(amount) FROM journal_lines WHERE ledger_id=:ledger AND role='expense'"
            ),
            {"ledger": ledger},
        )
    return Amount.from_decimal(total, fiat_asset(code="USD", scale=2)).to_string()


def rows(engine):
    """Compare PostgreSQL JSON text without decoding monetary NUMERIC into float."""
    with engine.connect() as connection:
        return {
            name: tuple(
                connection.scalars(
                    text(
                        f'SELECT row_to_json(r)::text FROM "{name}" r ORDER BY row_to_json(r)::text'
                    )
                )
            )
            for name in sorted(Base.metadata.tables)
        }


def completion():
    fields = [
        {
            "path": f"rows.0.0.0.{role}",
            "role": role,
            "value": value,
            "status": "certain",
            "evidence": [{"page": 0, "raw": "Fictional bank row " + value}],
        }
        for role, value in (("date", "2026-09-30"), ("currency", "USD"), ("amount", "100.00"))
    ]
    return Completion(
        summary={
            "pages": 1,
            "recognition": {
                "raw_text": "Fictional bank statement: 2026-09-30 USD 100.00; no OCR ran",
                "pages": [{"page_index": 0, "layer": "absent", "route": "render"}],
                "parsed": {"fields": fields},
            },
        },
        candidates=[
            Candidate(
                source_key="page:0/table:0/row:0",
                recognized={"version": 1, "kind": "statement_row", "fields": fields},
                evidence={"pages": [0]},
                fields={},
            )
        ],
    )


def seed_payment_evidence(engine, owner, files, store, content, upload):
    """Call before the existing interrupted-lease fixture is created."""
    scope = LedgerService(engine)
    entities = [
        scope.create_entity(
            owner,
            EntityCreate(
                kind="personal",
                name="Fictional evidence " + name,
                base_asset_id="USD",
                template_key="personal_default",
            ),
        )
        for name in ("target", "source")
    ]
    target, source = [entity.ledger.id for entity in entities]
    bank = scope.create_account(
        owner, target, AccountCreate(name="Fictional evidence bank", kind="bank", asset_ids=["USD"])
    )
    business = BusinessService(engine)
    party = business.create_party(
        owner, target, PartyCreate(id=uuid4(), name="Fictional evidence supplier", role="supplier")
    )
    category = next(row.id for row in scope.list_categories(owner, target) if row.kind == "expense")
    PostingService(engine).post_opening(
        owner,
        target,
        OpeningCreate(
            account_id=bank.id,
            asset_id="USD",
            amount="1000.00",
            transaction_date="2026-09-01",
            description="Fictional evidence opening",
        ),
        str(uuid4()),
    )
    document = DraftService(engine).create_draft(
        owner,
        target,
        BusinessDraftCreate(
            id=uuid4(),
            document_kind="bill",
            party_id=party.id,
            asset_id="USD",
            issue_date="2026-09-30",
            lines=[
                dict(
                    id=uuid4(),
                    description="Fictional service",
                    quantity="1",
                    unit_price="100.00",
                    tax_rate_percent="0",
                    category_id=category,
                    recognition_date="2026-09-30",
                )
            ],
        ),
    )
    BusinessConfirmationService(engine).confirm(
        owner,
        target,
        document.id,
        BusinessConfirmationCreate(id=uuid4(), expected_version=document.version),
        str(uuid4()),
    )
    payload = BusinessPaymentCreate(
        id=uuid4(),
        direction="payout",
        party_id=party.id,
        account_id=bank.id,
        asset_id="USD",
        amount="100.00",
        transaction_date="2026-09-30",
        fees=[],
        initial_allocation=dict(
            id=uuid4(),
            allocation_date="2026-09-30",
            lines=[
                dict(
                    document_id=document.id,
                    document_line_id=document.lines[0].id,
                    payment_amount="100.00",
                    document_amount="100.00",
                )
            ],
        ),
    )
    key = str(uuid4())
    raw_payment = payload.model_dump_json(exclude_none=True).encode()
    payment = BusinessSettlementService(engine).pay(
        owner, target, payload, key, raw_body=raw_payment
    )
    originals = [
        upload(files, store, owner, ledger, content, "Fictional bank statement 100 USD.pdf")
        for ledger in (target, source)
    ]
    queue, review = OcrQueueService(engine), DraftReviewService(engine)
    confirmations = []
    for original in originals:
        ledger, file = original["ledger"], original["receipt"].file_id
        job = queue.create_job(
            owner,
            ledger,
            JobCreate(intent_id=uuid4(), file_id=file),
            configuration={"lease_seconds": 300, "retry_seconds": 0, "processing": {}},
        )
        lease = queue.claim(owner)
        if lease is None or lease.job_id != job.id:
            raise RuntimeError("Fictional payment evidence claimed a different task")
        done = queue.finish(lease, completion())
        draft = done.result.draft_ids[0]
        observed = review.get_review(owner, ledger, draft)
        if not observed.fields or not all(field.requires_confirmation for field in observed.fields):
            raise RuntimeError("Fictional OCR fields bypassed manual confirmation")
        body = ConfirmationCreate(
            intent_id=uuid4(),
            expected_version=observed.version,
            target_ledger_id=target,
            target_file_id=originals[0]["receipt"].file_id,
            confirmed=[field.path for field in observed.fields],
            duplicate_ack=True,
            entry=dict(kind="link_payment", payment_id=payment.id, expected_version=1),
        )
        raw = body.model_dump_json().encode()
        before = rows(engine)
        result = ConfirmationService(engine).confirm(owner, ledger, draft, body, raw_body=raw)
        after = rows(engine)
        if (
            any(before[name] != after[name] for name in before if name not in ALLOWED)
            or result.payment != payment
        ):
            raise RuntimeError(
                "Payment evidence changed financial facts or its complete original receipt"
            )
        confirmations.append((ledger, draft, raw, result.model_dump_json()))
    read = BusinessSettlementReads(engine)
    if read.document_capacity(owner, target, document.id).outstanding_amount != "0.00":
        raise RuntimeError("Fictional B-05 bill was not fully settled")
    money = PostingService(engine).balances(owner, target)
    if len(money) != 1 or money[0].amount != "900.00":
        raise RuntimeError("Fictional B-05 bank evidence repeated the original deduction")
    payable = ControlAccounts(engine).balances(owner, target, system_key="payable.supplier")
    if (
        expense(engine, target) != "100.00"
        or not payable
        or any(row.amount != "0.00" for row in payable)
    ):
        raise RuntimeError("Fictional B-05 expense or supplier payable was duplicated")
    PeriodService(engine).change(
        owner,
        target,
        PeriodChange(
            action="close",
            expected_version=1,
            closed_through="2026-09-30",
            reason="Fictional evidence recovery",
        ),
        str(uuid4()),
    )
    business.update_party(owner, target, party.id, PartyUpdate(expected_version=1, archived=True))
    scope.update_account(owner, target, bank.id, AccountUpdate(expected_version=1, archived=True))
    for original, entity in zip(originals, entities, strict=True):
        files.update_file(
            owner,
            original["ledger"],
            original["receipt"].file_id,
            FileUpdate(expected_version=1, archived=True),
        )
        scope.update_entity(owner, entity.id, EntityUpdate(expected_version=1, archived=True))
    return dict(
        owner=owner,
        target=target,
        document=document.id,
        payload=payload,
        payment=payment,
        key=key,
        raw_payment=raw_payment,
        confirmations=confirmations,
        originals=originals,
        money=PostingService(engine).balances(owner, target),
        expense=expense(engine, target),
        controls=ControlAccounts(engine).balances(owner, target),
        capacity=read.document_capacity(owner, target, document.id),
        detail=read.get_payment(owner, target, payment.id),
    )


def verify_payment_evidence(engine, evidence):
    before = rows(engine)
    owner, target = evidence["owner"], evidence["target"]
    for ledger, draft, raw, expected in evidence["confirmations"]:
        body = ConfirmationCreate.model_validate_json(raw)
        if (
            ConfirmationService(engine)
            .confirm(owner, ledger, draft, body, raw_body=raw)
            .model_dump_json()
            != expected
            or ConfirmationReadService(engine)
            .get_confirmation(owner, ledger, draft)
            .model_dump_json()
            != expected
        ):
            raise RuntimeError("Restored payment evidence changed its permanent original receipt")
        changed = json.loads(raw)
        changed["entry"]["expected_version"] += 1
        changed_raw = json.dumps(changed).encode()
        try:
            ConfirmationService(engine).confirm(
                owner,
                ledger,
                draft,
                ConfirmationCreate.model_validate_json(changed_raw),
                raw_body=changed_raw,
            )
        except LedgerError as error:
            if error.code != "idempotency_conflict":
                raise RuntimeError(
                    "Restored payment evidence returned the wrong original-intent conflict"
                ) from None
        else:
            raise RuntimeError("Restored payment evidence accepted another original intent")
    if (
        BusinessSettlementService(engine).pay(
            owner, target, evidence["payload"], evidence["key"], raw_body=evidence["raw_payment"]
        )
        != evidence["payment"]
    ):
        raise RuntimeError("Restored payment original v2 receipt changed")
    read = BusinessSettlementReads(engine)
    if (
        read.get_payment(owner, target, evidence["payment"].id) != evidence["detail"]
        or read.document_capacity(owner, target, evidence["document"]) != evidence["capacity"]
        or PostingService(engine).balances(owner, target) != evidence["money"]
        or expense(engine, target) != evidence["expense"]
        or ControlAccounts(engine).balances(owner, target) != evidence["controls"]
        or rows(engine) != before
    ):
        raise RuntimeError("Restored evidence replay changed any table, financial fact or capacity")
