"""Compare one delivered page to its committed owner stream; no database or device mutation."""

from uuid import UUID

from coinpup_api.sync.schemas import ChangePage, ChangeRecord, cursor_value, notice_utc_time


class CheckpointConflict(ValueError):
    """The committed prefix does not match what the caller saved."""


def _facts(row: ChangeRecord):
    try:
        changed_at = notice_utc_time(row.changed_at)
    except ValueError:
        raise CheckpointConflict("Invalid change evidence timestamp.") from None
    return (
        row.seq,
        row.owner_id,
        row.ledger_id,
        row.entity_type,
        row.entity_id,
        row.entity_version,
        row.change_kind,
        changed_at,
    )


def matching_notice(left, right):
    return (
        isinstance(left, ChangeRecord)
        and isinstance(right, ChangeRecord)
        and _facts(left) == _facts(right)
    )


def verify_page(
    owner: UUID,
    *,
    after: str,
    through: str,
    limit: int,
    committed: ChangePage,
    received: ChangePage,
) -> str:
    """Caller must read 'committed' from its own snapshot at exactly these bounds and limit."""
    start, end = cursor_value(after), cursor_value(through)
    if end < start or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Invalid checkpoint window.")
    if not isinstance(owner, UUID):
        raise ValueError("An explicit owner is required.")
    if not isinstance(committed, ChangePage) or not isinstance(received, ChangePage):
        raise ValueError("Expected two validated pages.")
    rows = committed.changes
    if len(rows) != len(received.changes) or len(rows) > limit:
        raise CheckpointConflict("The committed page length differs.")
    cursor, previous = after, start
    for source, saved in zip(rows, received.changes, strict=True):
        seq = cursor_value(source.seq)
        if (
            not previous < seq <= end
            or source.owner_id != owner
            or not matching_notice(source, saved)
        ):
            raise CheckpointConflict("The committed notification differs.")
        previous, cursor = seq, source.seq
    if committed.next_cursor != cursor or received.next_cursor != cursor:
        raise CheckpointConflict("Only the last delivered notification can advance a checkpoint.")
    return cursor
