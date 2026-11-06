"""Complete fictional group transitions preserve stable members and terminal cancellation."""

from dataclasses import FrozenInstanceError, replace
from uuid import UUID

import pytest
from coinpup_api.business.credit_revision_plan import CreditMemberIdentity, plan_credit_membership
from coinpup_api.ledger.errors import MoneyError

LEDGER = UUID(int=1)
CREDIT = UUID(int=2)


def members(count=3):
    return tuple(
        CreditMemberIdentity(
            LEDGER,
            CREDIT,
            position,
            UUID(int=1000 + position),
            UUID(int=2000 + position),
            position,
            UUID(int=3000 + position),
        )
        for position in range(1, count + 1)
    )


def plan(current, sources):
    return plan_credit_membership(LEDGER, CREDIT, current, sources)


def test_complete_replacement_preserves_order_identity_and_individual_versions():
    original = members()
    added = UUID(int=4000)
    result = plan(original, (original[2].source_line_id, added, original[0].source_line_id))
    assert [row.action for row in result] == ["correct", "create", "correct", "cancel"]
    assert [row.replacement_line_no for row in result] == [1, 2, 3, None]
    assert [row.previous for row in result] == [original[2], None, original[0], original[1]]
    assert [row.next_operation_version for row in result] == [4, 1, 2, 3]
    assert [row.source_line_id for row in result] == [
        original[2].source_line_id,
        added,
        original[0].source_line_id,
        original[1].source_line_id,
    ]
    assert original == members()
    with pytest.raises(FrozenInstanceError):
        result[0].previous.operation_version = 9


def test_unchanged_members_are_all_corrected_not_silently_omitted():
    original = members()
    result = plan(original, tuple(row.source_line_id for row in original))
    assert len(result) == 3
    assert all(row.action == "correct" for row in result)
    assert tuple(row.previous for row in result) == original


def test_explicit_cancellation_accounts_for_every_old_member():
    original = members(200)
    result = plan(original, None)
    assert len(result) == 200
    assert all(row.action == "cancel" and row.replacement_line_no is None for row in result)
    assert tuple(row.previous for row in result) == original


def test_removed_source_reintroduced_later_does_not_reuse_cancelled_operation():
    original = members()
    after_first = (
        replace(original[0], operation_version=2, journal_id=UUID(int=5000)),
        replace(original[2], line_no=2, operation_version=4, journal_id=UUID(int=5001)),
    )
    result = plan(after_first, tuple(row.source_line_id for row in original))
    assert result[1].action == "create"
    assert result[1].previous is None
    assert result[1].next_operation_version == 1
    assert all(row.previous != original[1] for row in result)


def test_two_hundred_replacements_may_require_two_hundred_old_cancellations():
    original = members(200)
    new_sources = tuple(UUID(int=5000 + i) for i in range(200))
    result = plan(original, new_sources)
    assert len(result) == 400
    assert all(row.action == "create" and row.previous is None for row in result[:200])
    assert all(row.action == "cancel" for row in result[200:])
    assert [row.replacement_line_no for row in result[:200]] == list(range(1, 201))


@pytest.mark.parametrize(
    "sources",
    [
        (),
        [],
        [UUID(int=5000)],
        "fictional invalid source list",
        (None,),
        ("00000000-0000-0000-0000-000000000001",),
        (UUID(int=1), UUID(int=1)),
        tuple(UUID(int=5000 + i) for i in range(201)),
    ],
)
def test_invalid_replacement_never_becomes_accidental_cancellation(sources):
    with pytest.raises(MoneyError) as failure:
        plan(members(), sources)
    assert failure.value.code == "credit_revision_sources"


@pytest.mark.parametrize("field", ["source_line_id", "operation_id", "journal_id"])
def test_duplicate_current_identity_is_rejected(field):
    original = members()
    altered = (original[0], replace(original[1], **{field: getattr(original[0], field)}))
    with pytest.raises(MoneyError) as failure:
        plan(altered, None)
    assert failure.value.code == "credit_revision_integrity"


@pytest.mark.parametrize(
    "changes",
    [
        {"ledger_id": UUID(int=8)},
        {"credit_id": UUID(int=8)},
        {"line_no": 2},
        {"line_no": True},
        {"operation_version": True},
        {"operation_version": 0},
        {"operation_version": 2147483648},
        {"operation_version": "1"},
        {"source_line_id": "fictional source"},
        {"operation_id": None},
        {"journal_id": None},
    ],
)
def test_malformed_or_cross_scope_history_cannot_be_rewritten(changes):
    with pytest.raises(MoneyError) as failure:
        plan((replace(members(1)[0], **changes),), None)
    assert failure.value.code == "credit_revision_integrity"


@pytest.mark.parametrize("current", [(), [], [members(1)[0]], (None,), members(201)])
def test_active_group_requires_complete_bounded_immutable_membership(current):
    with pytest.raises(MoneyError) as failure:
        plan(current, None)
    assert failure.value.code == "credit_revision_integrity"


@pytest.mark.parametrize("sources", [None, (UUID(int=1001),), (UUID(int=5000),)])
def test_version_exhaustion_blocks_correction_removal_and_whole_cancellation(sources):
    current = (replace(members(1)[0], operation_version=2147483647),)
    with pytest.raises(MoneyError) as failure:
        plan(current, sources)
    assert failure.value.code == "credit_revision_version"


def test_last_representable_version_is_exact_and_does_not_follow_group_position():
    current = (replace(members(1)[0], operation_version=2147483646),)
    result = plan(current, (current[0].source_line_id,))
    assert result[0].next_operation_version == 2147483647
