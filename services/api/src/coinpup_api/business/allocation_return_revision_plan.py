"""Simulate an entire replacement and its retained payment, document and refund dependencies."""

from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import Date, Uuid, cast, column, func, select
from sqlalchemy.dialects.postgresql import ARRAY

from coinpup_api.business.allocation_return_plan import (
    AllocationReturnLinePlan,
    AllocationReturnPlan,
)
from coinpup_api.business.allocation_return_receipts import integrity
from coinpup_api.business.allocation_return_revision_current import CurrentReturnMember
from coinpup_api.business.allocation_return_revision_slots import (
    ReturnSlotChange,
    plan_source_slots,
)
from coinpup_api.business.allocation_return_sources import original_sources, plan_sources
from coinpup_api.business.settlement_rows import PostingGroup, allocation_rows
from coinpup_api.business.settlement_totals import document_totals, payment_totals
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.models import Account, AccountAsset, AssetRecord
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.period_checks import require_open_period
from coinpup_api.ledger.posting_storage import prepare_reversal_lines
from coinpup_api.ledger.service import LedgerError


@dataclass(frozen=True, slots=True)
class ReturnTransition:
    old: CurrentReturnMember | None
    reversal: PostingGroup | None
    posting: PostingGroup | None
    slot: ReturnSlotChange


def _assets(session, current, rows, requested):
    old = {line.asset_id for member in current.members for line in member.lines}
    new = {doc.asset_id for source, _, doc, _, _ in rows if source.id in requested}
    if requested:
        new.add(current.payment.asset_id)
    catalog = {
        record.asset_id: asset_definition(record)
        for record in session.scalars(
            select(AssetRecord)
            .where(AssetRecord.asset_id.in_(new | old))
            .order_by(AssetRecord.asset_id)
            .with_for_update(read=True)
        )
    }
    if set(catalog) != old | new:
        raise integrity()
    if any(not catalog[key].enabled for key in new):
        raise LedgerError("asset_disabled", 409, "An asset is disabled for new use.")
    return catalog


def _controls(session, ledger_id, plan):
    required = {(plan.advance_key, plan.payment_amount.asset.asset_id)}
    required.update(
        (plan.document_control_key, item.document_amount.asset.asset_id) for item in plan.lines
    )
    controls = {}
    rows = session.execute(
        select(Account, AccountAsset)
        .join(
            AccountAsset,
            (AccountAsset.account_id == Account.id) & (AccountAsset.ledger_id == Account.ledger_id),
        )
        .where(
            Account.ledger_id == ledger_id,
            Account.system_key.in_({key for key, asset in required}),
            AccountAsset.asset_id.in_({asset for key, asset in required}),
        )
    )
    for account, link in rows:
        if (account.system_key, link.asset_id) not in required:
            continue
        required.remove((account.system_key, link.asset_id))
        if account.archived:
            raise LedgerError("account_archived", 409, "Restore the account before new use.")
        if not link.enabled:
            raise LedgerError(
                "account_asset_disabled", 409, "Restore the asset link before new use."
            )
        controls[account.system_key] = account
    if required:
        raise integrity()
    return controls


def _require_dependencies(session, current, replacement):
    payment = current.payment
    references = func.public.coinpup_current_refund_return_references(
        cast(payment.ledger_id, Uuid), cast(payment.id, Uuid), cast([current.group.id], ARRAY(Uuid))
    ).table_valued(column("refund_date", Date))
    earliest = session.scalar(select(func.min(references.c.refund_date)))
    if earliest is not None:
        if replacement is None:
            raise LedgerError(
                "allocation_return_refund_reference",
                409,
                "Correct each current refund reference before cancelling the return.",
            )
        if replacement.return_date > earliest:
            raise LedgerError(
                "allocation_return_refund_date",
                409,
                "The return would follow a referencing refund.",
            )
    dates = [m.journal for m in current.members]
    if replacement:
        dates.append(
            SimpleNamespace(
                transaction_date=replacement.return_date, recognition_date=replacement.return_date
            )
        )
    require_open_period(session, payment.ledger_id, *dates)


def _capacity(session, current, payer, rows, sources, slots):
    ledger_id = current.group.ledger_id
    old = {m.fact["source_allocation_line_id"]: m.fact for m in current.members}
    payment_delta = 0
    changes = defaultdict(int)
    documents = {}
    for mapping, _, _, document_line, _ in rows:
        source = sources[mapping.id]
        asset = source.document_amount.asset
        line_id = document_line.id
        documents[line_id] = (document_line, asset)
        if mapping.id in old:
            fact = old[mapping.id]
            payment_delta += Amount.from_decimal(
                fact["payment_amount"], payer.amount.asset
            ).minor_units
            changes[line_id] += Amount.from_decimal(fact["document_amount"], asset).minor_units
        new = slots[mapping.id].replacement
        if new:
            payment_delta -= new.payment.minor_units
            changes[line_id] -= new.document.minor_units
    zero = Decimal(0)
    applied, refunded = payment_totals(session, ledger_id, [payer.id]).get(payer.id, (zero, zero))
    net = Amount.from_decimal(applied, payer.amount.asset).minor_units + payment_delta
    if (
        net < 0
        or net + Amount.from_decimal(refunded, payer.amount.asset).minor_units
        > payer.amount.minor_units
    ):
        raise LedgerError(
            "allocation_return_payment_capacity",
            409,
            "Current applications and refunds exceed the available principal.",
        )
    totals = document_totals(session, ledger_id, documents)
    for line_id, (line, asset) in documents.items():
        applied, credit_net, credit_tax = (
            Amount.from_decimal(x, asset).minor_units
            for x in totals.get(line_id, (zero, zero, zero))
        )
        net = applied + changes[line_id]
        if (
            net < 0
            or net + credit_net + credit_tax
            > Amount.from_decimal(line.total_amount, asset).minor_units
            or credit_net > Amount.from_decimal(line.net_amount, asset).minor_units
            or credit_tax > Amount.from_decimal(line.tax_amount, asset).minor_units
        ):
            raise LedgerError(
                "allocation_return_document_capacity",
                409,
                "Current applications and credits exceed the document line.",
            )


def plan_transition(session, current, replacement):
    payment = current.payment
    ledger_id = payment.ledger_id
    old = {m.fact["source_allocation_line_id"]: m for m in current.members}
    desired = (
        {line.source_allocation_line_id: line for line in replacement.lines} if replacement else {}
    )
    required = {
        key: SimpleNamespace(
            source_allocation_line_id=key, expected_version=m.fact["source_version"]
        )
        for key, m in old.items()
    }
    rows = original_sources(
        session,
        ledger_id,
        SimpleNamespace(payment_id=payment.id, lines=(required | desired).values()),
    )
    catalog = _assets(session, current, rows, desired)
    _require_dependencies(session, current, replacement)
    payer, sources, _ = plan_sources(session, ledger_id, payment, rows, catalog)
    slots = plan_source_slots(
        session, ledger_id, payment.id, current.group.id, sources, desired.values()
    )
    _capacity(session, current, payer, rows, sources, slots)
    postings = {}
    receipt = payment.direction == "receipt"
    if replacement:
        lines = []
        for position, key in enumerate(desired, 1):
            source, share = sources[key], slots[key].replacement
            if source.party_id != payment.party_id or source.document_kind != (
                "invoice" if receipt else "bill"
            ):
                raise integrity()
            if replacement.return_date < source.allocation_date:
                raise LedgerError(
                    "allocation_return_date", 422, "Return cannot precede its original application."
                )
            lines.append(
                AllocationReturnLinePlan(
                    position,
                    source,
                    share.payment,
                    share.document,
                    -share.payment if receipt else share.payment,
                    share.document if receipt else -share.document,
                )
            )
        plan = AllocationReturnPlan(
            "advance.received" if receipt else "advance.paid",
            "receivable.customer" if receipt else "payable.supplier",
            replacement.return_date,
            Amount(payer.amount.asset, sum(line.payment_amount.minor_units for line in lines)),
            tuple(lines),
        )
        postings = dict(
            zip(
                desired,
                allocation_rows(
                    ledger_id,
                    payment.party_id,
                    plan,
                    _controls(session, ledger_id, plan),
                    {},
                    catalog,
                    description=replacement.reason,
                ),
                strict=True,
            )
        )
    transitions = {}
    for key in dict.fromkeys((*desired, *old)):
        member, posting = old.get(key), postings.get(key)
        reversal = None
        if member:
            identifier = uuid4()
            lines, parameters = prepare_reversal_lines(member.lines, identifier, catalog)
            reversal = PostingGroup(
                member.operation.id,
                identifier,
                member.journal.description,
                tuple(lines),
                tuple(parameters),
            )
            if posting:
                posting = replace(posting, operation_id=member.operation.id)
        transitions[key] = ReturnTransition(member, reversal, posting, slots[key])
    return catalog, sources, transitions
