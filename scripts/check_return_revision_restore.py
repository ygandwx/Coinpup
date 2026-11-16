"""Round-trip sealed return postings, permanent source order and versioned refund evidence."""

import json
from uuid import uuid4

from check_business_settlement_restore import seed_business_settlements
from coinpup_api.business.allocation_return_current_read import BusinessAllocationReturnCurrentRead
from coinpup_api.business.allocation_return_reads import BusinessAllocationReturnReads
from coinpup_api.business.allocation_return_revision_history import BusinessAllocationReturnHistory
from coinpup_api.business.allocation_return_revision_schemas import (
    AllocationReturnCancellationCreate,
    AllocationReturnCorrectionCreate,
)
from coinpup_api.business.allocation_return_revisions import BusinessAllocationReturnRevisionService
from coinpup_api.business.allocation_return_schemas import BusinessAllocationReturnCreate
from coinpup_api.business.allocation_returns import BusinessAllocationReturnService
from coinpup_api.business.refund_models import BusinessRefundReturn
from coinpup_api.business.refund_revision_history import BusinessRefundHistory
from coinpup_api.business.refund_revision_return_models import BusinessRefundRevisionReturn
from coinpup_api.business.refund_revision_schemas import RefundCorrectionCreate
from coinpup_api.business.refund_revisions import BusinessRefundRevisionService
from coinpup_api.business.refund_schemas import BusinessRefundCreate
from coinpup_api.business.refunds import BusinessRefundService
from coinpup_api.files.service import DocumentService
from coinpup_api.ledger.models import Entity, Ledger
from coinpup_api.ledger.period_models import LedgerPeriod
from coinpup_api.ledger.period_schemas import PeriodChange
from coinpup_api.ledger.period_service import PeriodService
from coinpup_api.ledger.schemas import AccountUpdate, EntityUpdate
from coinpup_api.ledger.service import LedgerError, LedgerService
from sqlalchemy import select


def _invoke(engine, owner, ledger, case):
    service, method, target, payload, key, raw = case
    return getattr(service(engine), method)(owner, ledger, *target, payload, key, raw_body=raw)


def _state(engine, owner, ledger, primary, auxiliary, refund):
    history = BusinessAllocationReturnHistory(engine)
    current = BusinessAllocationReturnCurrentRead(engine)
    reads = BusinessAllocationReturnReads(engine)
    with engine.connect() as c:
        original = c.execute(
            select(BusinessRefundReturn.return_id, BusinessRefundReturn.return_version).where(
                BusinessRefundReturn.ledger_id == ledger, BusinessRefundReturn.refund_id == refund
            )
        ).one()
        revised = c.execute(
            select(
                BusinessRefundRevisionReturn.return_id, BusinessRefundRevisionReturn.return_version
            ).where(
                BusinessRefundRevisionReturn.ledger_id == ledger,
                BusinessRefundRevisionReturn.refund_id == refund,
            )
        ).one()
    return dict(
        groups=[current.current(owner, ledger, i) for i in (primary, auxiliary)],
        history=[history.list_history(owner, ledger, i) for i in (primary, auxiliary)],
        originals=[reads.get_return(owner, ledger, i) for i in (primary, auxiliary)],
        refund_history=BusinessRefundHistory(engine).list_history(owner, ledger, refund),
        refund_bindings=[tuple(original), tuple(revised)],
    )


def seed_return_revisions(engine, owner, files, store, pdf, upload):
    base = seed_business_settlements(engine, owner)
    ledger = base["ledger"]
    payment = next(
        c["payload"]
        for c in base["cases"]
        if c["method"] == "pay"
        and c["payload"].direction == "payout"
        and c["payload"].initial_allocation
    )
    later = next(
        c["payload"]
        for c in base["cases"]
        if c["method"] == "allocate" and c["payload"].payment_id == payment.id
    )
    structure = LedgerService(engine)
    with engine.connect() as c:
        entity, version = c.execute(
            select(Entity.id, Entity.version)
            .join(Ledger, Ledger.entity_id == Entity.id)
            .where(Ledger.id == ledger)
        ).one()
        period_version = c.scalar(
            select(LedgerPeriod.version).where(LedgerPeriod.ledger_id == ledger)
        )
    opened = structure.update_entity(
        owner, entity, EntityUpdate(expected_version=version, archived=False)
    )
    bank = next(
        r
        for r in structure.list_accounts(owner, ledger, include_archived=True)
        if r.id == payment.account_id
    )
    active_bank = structure.update_account(
        owner, ledger, bank.id, AccountUpdate(expected_version=bank.version, archived=False)
    )
    reads = BusinessAllocationReturnReads(engine)
    a, b = [
        reads.capacity(owner, ledger, group).lines[0]
        for group in (payment.initial_allocation.id, later.id)
    ]
    if a.document_asset_id != "EUR" or b.document_asset_id != "EUR":
        raise RuntimeError(
            "Cross-asset return backup fixture requires its two original EUR sources."
        )

    def line(source):
        return dict(
            source_allocation_line_id=source.source_allocation_line_id,
            expected_version=1,
            payment_amount="11.00",
            document_amount="10.00",
        )

    cases = []

    def record(service, method, target, payload, key):
        raw = json.dumps(
            payload.model_dump(mode="json") | {"reason": " " + payload.reason + " "},
            ensure_ascii=False,
            indent=2,
        ).encode()
        case = service, method, target, payload, key, raw
        result = _invoke(engine, owner, ledger, case)
        cases.append((case, result))
        return result

    def create(source, key):
        return record(
            BusinessAllocationReturnService,
            "create",
            (),
            BusinessAllocationReturnCreate(
                id=uuid4(),
                payment_id=payment.id,
                return_date="2026-10-03",
                reason="Fictional return recovery",
                lines=[line(source)],
            ),
            key,
        )

    original = create(a, "fictional-return-primary")

    def correct(version, source, key):
        payload = AllocationReturnCorrectionCreate(
            expected_version=version,
            reason="Fictional correct source",
            replacement=dict(
                return_date="2026-10-04",
                reason="Fictional revised return 中文",
                lines=[line(source)],
            ),
        )
        return record(
            BusinessAllocationReturnRevisionService, "correct", (original.id,), payload, key
        )

    first = correct(1, b, "fictional-swap-source")
    born = first.members[0].operation_id
    attached = upload(
        files, store, owner, ledger, pdf, "Fictional return revision evidence.pdf", operation=born
    )
    cash = BusinessRefundCreate(
        id=uuid4(),
        expected_version=1,
        payment_id=payment.id,
        account_id=bank.id,
        asset_id="USD",
        amount="1.00",
        payment_amount="1.00",
        transaction_date="2026-10-06",
        reason="Fictional linked cash",
        allocation_return_ids=[original.id],
    )
    issued = record(BusinessRefundService, "create", (), cash, "fictional-linked-cash")
    rejoined = correct(2, a, "fictional-rejoin-source")
    if (
        rejoined.members[0].source_return_no != 2
        or rejoined.members[0].operation_id == original.postings[0].operation_id
    ):
        raise RuntimeError("A retired source must be assigned a fresh permanent slot on rejoining.")
    auxiliary = create(b, "fictional-independent-return")
    end = AllocationReturnCancellationCreate(
        expected_version=3, reason="Fictional terminal cancellation"
    )
    try:
        BusinessAllocationReturnRevisionService(engine).cancel(
            owner, ledger, original.id, end, "fictional-end-primary"
        )
    except LedgerError as error:
        if (error.status, error.code) != (409, "allocation_return_refund_reference"):
            raise
    else:
        raise RuntimeError(
            "An effective refund reference unexpectedly allowed return cancellation."
        )
    edit = RefundCorrectionCreate(
        expected_version=1,
        reason="Fictional explicit evidence correction",
        replacement=cash.model_dump(exclude={"id", "payment_id", "expected_version"})
        | {"allocation_return_ids": [auxiliary.id]},
    )
    record(
        BusinessRefundRevisionService,
        "correct",
        (issued.id,),
        edit,
        "fictional-replace-cash-evidence",
    )
    record(
        BusinessAllocationReturnRevisionService,
        "cancel",
        (original.id,),
        end,
        "fictional-end-primary",
    )
    PeriodService(engine).change(
        owner,
        ledger,
        PeriodChange(
            action="close",
            expected_version=period_version,
            closed_through="2026-10-06",
            reason="Fictional close after return revisions",
        ),
        "fictional-close-revised-returns",
    )
    structure.update_account(
        owner, ledger, bank.id, AccountUpdate(expected_version=active_bank.version, archived=True)
    )
    structure.update_entity(
        owner, entity, EntityUpdate(expected_version=opened.version, archived=True)
    )
    expected = _state(engine, owner, ledger, original.id, auxiliary.id, issued.id)
    if (
        expected["refund_bindings"] != [(original.id, 2), (auxiliary.id, 1)]
        or expected["groups"][0].status != "cancelled"
        or expected["groups"][1].members[0].source_return_no != 2
    ):
        raise RuntimeError(
            "The fictional final state is missing its original and replacement evidence."
        )
    return dict(
        owner=owner,
        ledger=ledger,
        primary=original.id,
        auxiliary=auxiliary.id,
        refund=issued.id,
        cases=cases,
        expected=expected,
        attachment=attached,
        born=born,
        links=files.list_operation_files(owner, ledger, born),
    )


def verify_return_revisions(engine, evidence):
    e = evidence
    owner, ledger = e["owner"], e["ledger"]
    if _state(engine, owner, ledger, e["primary"], e["auxiliary"], e["refund"]) != e["expected"]:
        raise RuntimeError("Restored return history, current facts or refund bindings differ.")
    for case, expected in e["cases"]:
        if _invoke(engine, owner, ledger, case) != expected:
            raise RuntimeError(
                "Restored allocation return or linked refund lost its original receipt."
            )
        if case[0] is BusinessAllocationReturnRevisionService:
            actual = BusinessAllocationReturnHistory(engine).get_revision(
                owner, ledger, e["primary"], expected.revision_id
            )
            if actual != expected:
                raise RuntimeError(
                    "Restored revision detail differs from the fixed return receipt."
                )
    if DocumentService(engine).list_operation_files(owner, ledger, e["born"]) != e["links"]:
        raise RuntimeError("A removed revision-born return member lost its private evidence link.")
