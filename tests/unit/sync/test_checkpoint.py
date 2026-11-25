from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from coinpup_api.sync.checkpoint import CheckpointConflict, verify_page
from coinpup_api.sync.schemas import ChangePage, ChangeRecord, SavedNotice
from pydantic import ValidationError

OWNER, LEDGER = UUID(int=1), UUID(int=2)
START, A, B, END = (str(2**53 + i) for i in (1, 3, 7, 10))


def row(seq):
    return ChangeRecord(
        seq=seq,
        owner_id=OWNER,
        ledger_id=LEDGER,
        entity_type="financial_operations",
        entity_id=str(UUID(int=3)),
        entity_version=3,
        change_kind="cancel",
        changed_at=datetime(2026, 1, 31, microsecond=123456, tzinfo=UTC),
    )


def page(*rows, cursor=None):
    return ChangePage(changes=list(rows), next_cursor=cursor or (rows[-1].seq if rows else START))


def check(saved, source=None, **kwargs):
    return verify_page(
        kwargs.pop("owner", OWNER),
        after=kwargs.pop("after", START),
        through=kwargs.pop("through", END),
        limit=kwargs.pop("limit", 2),
        committed=source if source is not None else page(row(A), row(B)),
        received=saved,
        **kwargs,
    )


def test_exact_committed_prefix_with_holes_and_equivalent_zoned_timestamp():
    prior = row(B)
    at = prior.changed_at.astimezone(timezone(timedelta(hours=8))).isoformat()
    saved = page(row(A), SavedNotice.model_validate(prior.model_dump() | {"changed_at": at}))
    assert check(saved) == B
    assert check(page(), page()) == START
    assert check(page(cursor="0"), page(cursor="0"), after="0") == "0"


@pytest.mark.parametrize(
    "value",
    [
        "1",
        "1769817600.123456",
        "0001-01-01T00:00:00+00:01",
        "9999-12-31T23:59:59-00:01",
    ],
)
def test_saved_notice_rejects_implicit_epoch_and_utc_overflow(value):
    with pytest.raises(ValidationError):
        SavedNotice.model_validate(row(A).model_dump() | {"changed_at": value})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("owner_id", UUID(int=4)),
        ("ledger_id", None),
        ("entity_type", "stored_files"),
        ("entity_id", str(UUID(int=4))),
        ("entity_version", 2),
        ("change_kind", "upsert"),
        ("changed_at", datetime(2026, 1, 31, microsecond=123455, tzinfo=UTC)),
        ("changed_at", datetime(2026, 1, 31)),
        ("changed_at", datetime(1, 1, 1, tzinfo=timezone(timedelta(minutes=1)))),
        ("changed_at", datetime(9999, 12, 31, 23, 59, tzinfo=timezone(timedelta(hours=-1)))),
        ("seq", str(int(A) + 1)),
    ],
)
def test_same_last_cursor_cannot_hide_a_changed_earlier_notification(key, value):
    with pytest.raises(CheckpointConflict):
        check(page(row(A).model_copy(update={key: value}), row(B)))


def test_do_not_accept_truncated_reordered_duplicate_or_future_pages():
    for saved in (page(row(B)), page(row(B), row(A)), page(row(A), row(A)), page(row(A), row(END))):
        with pytest.raises(CheckpointConflict):
            check(saved)
    for malformed in (page(row(A), row(A)), page(row(A), row(str(int(END) + 1)))):
        with pytest.raises(CheckpointConflict):
            check(malformed, malformed)
    for tail in (START, END):
        with pytest.raises(CheckpointConflict):
            check(page(row(A), row(B), cursor=tail))
    with pytest.raises(CheckpointConflict):
        check(page(), page(cursor=END))
    with pytest.raises(CheckpointConflict):
        check(page(row(A), row(B)), owner=UUID(int=33))


@pytest.mark.parametrize(
    "kwargs", [{"after": "01"}, {"through": "0"}, {"limit": True}, {"limit": 201}]
)
def test_invalid_windows_are_not_clamped(kwargs):
    with pytest.raises(ValueError):
        check(page(), page(), **kwargs)
