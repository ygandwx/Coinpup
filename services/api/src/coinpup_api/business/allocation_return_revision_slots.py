"""Plan replacements and retirements against the permanent order of their original sources."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import BigInteger, Uuid, cast, column, func, select
from sqlalchemy.dialects.postgresql import ARRAY

from coinpup_api.business.allocation_return_projections import current_return_members
from coinpup_api.business.allocation_return_receipts import integrity
from coinpup_api.business.allocation_return_revision_sequence import (
    ReturnSequencePlan,
    ReturnShare,
    ReturnSourceSlot,
    plan_return_source_revision,
)
from coinpup_api.ledger.money import Amount


@dataclass(frozen=True, slots=True)
class ReturnSlotChange:
    sequence: ReturnSequencePlan
    predecessor: UUID | None
    replacement: ReturnShare | None
    absorbs_remainder: bool | None


def _recorded_slots(session, ledger_id, identifiers):
    slots = func.public.coinpup_return_source_slots(
        cast(ledger_id, Uuid), cast(identifiers, ARRAY(Uuid))
    ).table_valued(
        *(
            column(name, Uuid)
            for name in (
                "source_id",
                "return_id",
                "operation_id",
                "revision_id",
                "declared_predecessor",
                "predecessor",
            )
        ),
        *(column(name, BigInteger) for name in ("source_return_no", "assigned_no", "birth_events")),
    )
    return session.execute(
        select(slots).order_by(slots.c.source_id, slots.c.assigned_no)
    ).mappings()


def plan_source_slots(session, ledger_id, payment_id, return_id, sources, lines):
    """The caller supplies the scoped union of old and requested sources after the write lock."""
    requested = {line.source_allocation_line_id: line for line in lines}
    identifiers = list(sources)
    if (
        not 1 <= len(identifiers) <= 400
        or len(requested) != len(lines)
        or not requested.keys() <= sources.keys()
        or any(
            s.id != key or s.ledger_id != ledger_id or s.payment_id != payment_id
            for key, s in sources.items()
        )
    ):
        raise integrity()
    active = current_return_members(ledger_id, identifiers)
    shares = {row.operation_id: row for row in session.execute(select(active))}
    history = {key: [] for key in identifiers}
    operations = {key: [] for key in identifiers}
    for row in _recorded_slots(session, ledger_id, identifiers):
        key = row.source_id
        ordered = operations[key]
        predecessor = ordered[-1] if ordered else None
        if (
            row.birth_events != 1
            or row.source_return_no != len(ordered) + 1
            or row.assigned_no != row.source_return_no
            or row.predecessor != predecessor
            or (row.revision_id is not None and row.declared_predecessor != predecessor)
        ):
            raise integrity()
        current = shares.pop(row.operation_id, None)
        share = None
        if current is not None:
            if (current.source_allocation_line_id, current.return_id, current.source_return_no) != (
                key,
                row.return_id,
                row.source_return_no,
            ):
                raise integrity()
            share = ReturnShare(
                Amount.from_decimal(current.payment_amount, sources[key].payment_amount.asset),
                Amount.from_decimal(current.document_amount, sources[key].document_amount.asset),
            )
        history[key].append(ReturnSourceSlot(row.source_return_no, row.return_id, share))
        ordered.append(row.operation_id)
    if shares:
        raise integrity()

    changes = {}
    for key, source in sources.items():
        desired = requested.get(key)
        replacement = None
        if desired is not None:
            replacement = ReturnShare(
                Amount.parse(desired.payment_amount, source.payment_amount.asset),
                Amount.parse(desired.document_amount, source.document_amount.asset),
            )
        plan = plan_return_source_revision(
            ReturnShare(source.payment_amount, source.document_amount),
            tuple(history[key]),
            return_id,
            replacement,
        )
        earlier = plan.source_return_no - 2
        changes[key] = ReturnSlotChange(
            plan,
            operations[key][earlier] if earlier >= 0 else None,
            replacement,
            plan.previous_document + replacement.document == source.document_amount
            if replacement
            else None,
        )
    return changes
