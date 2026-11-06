"""Plan complete credit membership changes without posting or inventing new identities."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from coinpup_api.ledger.errors import MoneyError

_MAX_VERSION = 2147483647


@dataclass(frozen=True, slots=True)
class CreditMemberIdentity:
    ledger_id: UUID
    credit_id: UUID
    line_no: int
    source_line_id: UUID
    operation_id: UUID
    operation_version: int
    journal_id: UUID


@dataclass(frozen=True, slots=True)
class CreditMemberChange:
    action: Literal["correct", "create", "cancel"]
    source_line_id: UUID
    replacement_line_no: int | None
    previous: CreditMemberIdentity | None

    @property
    def next_operation_version(self) -> int:
        return 1 if self.previous is None else self.previous.operation_version + 1


def plan_credit_membership(
    ledger_id: UUID,
    credit_id: UUID,
    current: tuple[CreditMemberIdentity, ...],
    replacement_sources: tuple[UUID, ...] | None,
) -> tuple[CreditMemberChange, ...]:
    """None cancels the whole group; an empty replacement is never implicit cancellation.

    The caller must read the current active group under the normal write lock and
    validate its expected version, periods, amounts and capacity separately. New
    member IDs are allocated by that transaction, never reused from cancelled history.
    All retained members are corrected, including ones whose amounts are unchanged.
    """
    if (
        not isinstance(ledger_id, UUID)
        or not isinstance(credit_id, UUID)
        or not isinstance(current, tuple)
        or not 1 <= len(current) <= 200
    ):
        raise MoneyError("credit_revision_integrity", "The current credit group is inconsistent.")
    for position, member in enumerate(current, 1):
        if (
            not isinstance(member, CreditMemberIdentity)
            or member.ledger_id != ledger_id
            or member.credit_id != credit_id
            or type(member.line_no) is not int
            or member.line_no != position
            or type(member.operation_version) is not int
            or not 1 <= member.operation_version <= _MAX_VERSION
            or any(
                not isinstance(value, UUID)
                for value in (member.source_line_id, member.operation_id, member.journal_id)
            )
        ):
            raise MoneyError(
                "credit_revision_integrity", "The current credit membership is inconsistent."
            )
    for name in ("source_line_id", "operation_id", "journal_id"):
        if len({getattr(member, name) for member in current}) != len(current):
            raise MoneyError(
                "credit_revision_integrity", "Current credit member identities must be distinct."
            )
    if replacement_sources is not None and (
        not isinstance(replacement_sources, tuple)
        or not 1 <= len(replacement_sources) <= 200
        or any(not isinstance(source, UUID) for source in replacement_sources)
        or len(set(replacement_sources)) != len(replacement_sources)
    ):
        raise MoneyError(
            "credit_revision_sources", "Use a nonempty ordered set of replacement source lines."
        )
    if any(member.operation_version == _MAX_VERSION for member in current):
        raise MoneyError(
            "credit_revision_version", "A current member cannot advance to another stored version."
        )
    by_source = {member.source_line_id: member for member in current}
    requested = replacement_sources if replacement_sources is not None else ()
    changes = [
        CreditMemberChange(
            "correct" if source in by_source else "create",
            source,
            position,
            by_source.get(source),
        )
        for position, source in enumerate(requested, 1)
    ]
    retained = set(requested)
    changes.extend(
        CreditMemberChange("cancel", member.source_line_id, None, member)
        for member in current
        if member.source_line_id not in retained
    )
    return tuple(changes)
