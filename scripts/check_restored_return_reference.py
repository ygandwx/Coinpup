"""Exercise explicit refund evidence changes on an already verified disposable restored copy."""

from uuid import uuid4

from _database_archive import ArchiveError
from check_backup_restore import snapshot
from check_return_revision_restore import _invoke, _state
from coinpup_api.business.allocation_return_revision_schemas import (
    AllocationReturnCancellationCreate,
)
from coinpup_api.business.allocation_return_revisions import BusinessAllocationReturnRevisionService
from coinpup_api.business.allocation_returns import BusinessAllocationReturnService
from coinpup_api.business.refund_reads import BusinessRefundReads
from coinpup_api.business.refund_revision_schemas import RefundCorrectionCreate
from coinpup_api.business.refund_revisions import BusinessRefundRevisionService
from coinpup_api.files.service import DocumentService
from coinpup_api.ledger.models import Entity, Ledger
from coinpup_api.ledger.period_models import LedgerPeriod
from coinpup_api.ledger.period_schemas import PeriodChange
from coinpup_api.ledger.period_service import PeriodService
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.schemas import AccountUpdate, EntityUpdate
from coinpup_api.ledger.service import LedgerError, LedgerService
from sqlalchemy import select


def exercise_restored_return_reference(engine, target, e):
    owner, ledger = e["owner"], e["ledger"]
    group, aux, refund = e["primary"], e["auxiliary"], e["refund"]
    if _state(engine, owner, ledger, group, aux, refund) != e["expected"]:
        raise ArchiveError("The restored return continuation did not start at the verified state.")
    (old_edit,) = [
        case[3]
        for case, _ in e["cases"]
        if case[0] is BusinessRefundRevisionService and case[1] == "correct"
    ]
    structure = LedgerService(engine)
    with engine.connect() as c:
        entity, version = c.execute(
            select(Entity.id, Entity.version)
            .join(Ledger, Ledger.entity_id == Entity.id)
            .where(Ledger.id == ledger, Entity.owner_id == owner)
        ).one()
        period = c.scalar(select(LedgerPeriod.version).where(LedgerPeriod.ledger_id == ledger))
    structure.update_entity(owner, entity, EntityUpdate(expected_version=version, archived=False))
    bank = next(
        a
        for a in structure.list_accounts(owner, ledger, include_archived=True)
        if a.id == old_edit.replacement.account_id
    )
    structure.update_account(
        owner, ledger, bank.id, AccountUpdate(expected_version=bank.version, archived=False)
    )
    PeriodService(engine).change(
        owner,
        ledger,
        PeriodChange(
            action="reopen",
            expected_version=period,
            closed_through="2026-09-30",
            reason="Fictional restored October continuation",
        ),
        "fictional-restored-return-reopen",
    )
    reads = BusinessRefundReads(engine)
    active = reads.current(owner, ledger, refund)
    if active.version != 2 or active.fact.allocation_return_ids != (aux,):
        raise ArchiveError("The returned group's current refund reference was not restored.")
    balances = PostingService(engine).balances(owner, ledger, bank.id)
    end = AllocationReturnCancellationCreate(
        expected_version=e["expected"]["groups"][1].version,
        reason="Fictional restored return cancellation",
    )
    end_case = (
        BusinessAllocationReturnRevisionService,
        "cancel",
        (aux,),
        end,
        "fictional-restored-terminal-key",
        end.model_dump_json().encode(),
    )
    before = snapshot(target)
    try:
        _invoke(engine, owner, ledger, end_case)
    except LedgerError as exc:
        if (exc.status, exc.code) != (409, "allocation_return_refund_reference"):
            raise
    else:
        raise ArchiveError("An effective restored refund reference was silently detached.")
    if snapshot(target) != before:
        raise ArchiveError("A rejected restored cancellation wrote data.")
    release = RefundCorrectionCreate.model_validate(
        dict(
            expected_version=active.version,
            reason="Fictional explicit release after restoring",
            replacement=old_edit.replacement.model_dump(mode="json")
            | {"allocation_return_ids": []},
        )
    )
    release_case = (
        BusinessRefundRevisionService,
        "correct",
        (refund,),
        release,
        "fictional-restored-explicit-release",
        release.model_dump_json().encode(),
    )
    fixed = _invoke(engine, owner, ledger, release_case)
    before_capacity = snapshot(target)
    try:
        _invoke(engine, owner, ledger, end_case)
    except LedgerError as exc:
        if (exc.status, exc.code) != (409, "allocation_return_payment_capacity"):
            raise
    else:
        raise ArchiveError("Clearing evidence unexpectedly freed payment principal.")
    if snapshot(target) != before_capacity:
        raise ArchiveError("A rejected over-capacity cancellation wrote data.")

    # The USD 110 payment was fully applied before the returns. Its USD 1 refund
    # remains effective even after removing the evidence link. Return a separate
    # USD 11 / EUR 10 application explicitly before reinstating the target one.
    original = next(
        case[3]
        for case, _ in e["cases"]
        if case[0] is BusinessAllocationReturnService and case[3].id == group
    )
    independent = type(original).model_validate(
        original.model_dump()
        | {"id": uuid4(), "reason": "Fictional separate restored application return"}
    )
    independent_case = (
        BusinessAllocationReturnService,
        "create",
        (),
        independent,
        "fictional-restored-independent-return",
        independent.model_dump_json().encode(),
    )
    independent_receipt = _invoke(engine, owner, ledger, independent_case)
    if independent_receipt.id in {aux, group} or len(independent_receipt.postings) != 1:
        raise ArchiveError("Separate restored capacity did not come from a new return.")
    ended = _invoke(engine, owner, ledger, end_case)
    current = reads.current(owner, ledger, refund)
    state = _state(engine, owner, ledger, group, aux, refund)
    if (
        fixed.version != 3
        or ended.version != 2
        or current.fact.allocation_return_ids != ()
        or [v.status for v in state["groups"]] != ["cancelled", "cancelled"]
        or state["refund_bindings"] != e["expected"]["refund_bindings"]
        or state["history"][0] != e["expected"]["history"][0]
        or state["history"][1][:-1] != e["expected"]["history"][1]
        or state["refund_history"][:-1] != e["expected"]["refund_history"]
        or balances != PostingService(engine).balances(owner, ledger, bank.id)
        or DocumentService(engine).list_operation_files(owner, ledger, e["born"]) != e["links"]
    ):
        raise ArchiveError("Continuation changed original refund bindings, files or cash.")
    after = snapshot(target)
    for case, saved in [
        *e["cases"],
        (release_case, fixed),
        (independent_case, independent_receipt),
        (end_case, ended),
    ]:
        if _invoke(engine, owner, ledger, case) != saved:
            raise ArchiveError("Restored original intent did not replay.")
    if snapshot(target) != after:
        raise ArchiveError("A restored continuation replay wrote data.")
    print(
        "Restored refund evidence blocked return cancellation with zero changed rows; "
        "explicit release: refund 2 to 3; payment capacity separately refused with zero writes; "
        "independent USD 11 / EUR 10 return; original cancellation key: return 1 to 2; "
        "historical bindings, private file, cash and all original receipts unchanged."
    )
