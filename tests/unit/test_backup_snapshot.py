"""Backup comparison must not omit new tables or round exact stored quantities."""

import sys
from contextlib import nullcontext
from copy import deepcopy
from datetime import date
from decimal import Decimal, Inexact, localcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from _database_archive import ArchiveError  # noqa: E402
from check_backup_restore import (  # noqa: E402
    snapshot,
    verify_revision_history,
    verify_sealed_journal,
)


def test_snapshot_includes_new_tables_and_keeps_numeric_json_as_exact_text():
    precise = '{"amount":99999999999999999999.999999999999999999}'
    rows = {
        "administrators": [('{"id":"fictional-admin"}',)],
        "future_ledger_entries": [(precise,)],
        "empty_future_table": [],
    }
    queried = []

    class Connection:
        def execute(self, query):
            if isinstance(query, str):
                if query.startswith("SET LOCAL"):
                    return None
                assert "information_schema.tables" in query
                return SimpleNamespace(fetchall=lambda: [(name,) for name in rows])
            rendered = query.as_string()
            assert "row_to_json(t)::text" in rendered
            assert "ORDER BY row_to_json(t)::text" in rendered
            name = next(name for name in rows if f'"public"."{name}"' in rendered)
            queried.append(name)
            return SimpleNamespace(fetchall=lambda: rows[name])

    target = SimpleNamespace(connect=lambda: nullcontext(Connection()))
    result = snapshot(target)
    assert result == rows
    assert set(queried) == set(rows)
    assert result["future_ledger_entries"][0][0] == precise
    assert isinstance(result["future_ledger_entries"][0][0], str)


@pytest.mark.parametrize("behavior", ["sealed", "wrong_constraint", "allowed"])
@pytest.mark.parametrize("component_no", [None, 1])
def test_sealed_probe_always_rolls_back_and_rejects_false_positive_constraints(
    behavior, component_no
):
    journal_id = uuid4()
    originals = [
        {
            "id": uuid4(),
            "journal_id": journal_id,
            "line_no": index,
            "amount": Decimal(amount),
            "component_no": 0 if index <= 2 else 1,
        }
        for index, amount in enumerate(["1.00", "-1.00", "0.01", "-0.01"], start=1)
    ]
    rolled_back = []
    attempted = []

    class DriverError(Exception):
        sqlstate = "23514" if behavior == "sealed" else "23505"
        diag = SimpleNamespace(
            constraint_name="ck_journal_sealed" if behavior == "sealed" else "a_unique_constraint"
        )

    class Connection:
        def begin(self):
            # Deliberately has no commit method: the probe must never commit even on success.
            return SimpleNamespace(rollback=lambda: rolled_back.append(True))

        def execute(self, statement, parameters=None):
            if parameters is None and not attempted:
                return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: originals))
            if parameters is not None:
                attempted.extend(parameters)
                if behavior != "allowed":
                    raise IntegrityError("private fixture statement", {}, DriverError())

    engine = SimpleNamespace(connect=lambda: nullcontext(Connection()))
    if behavior == "sealed":
        verify_sealed_journal(engine, journal_id, component_no)
    else:
        message = "unexpectedly allowed" if behavior == "allowed" else "unexpected reason"
        with pytest.raises(ArchiveError, match=message):
            verify_sealed_journal(engine, journal_id, component_no)
    assert rolled_back == [True]
    selected = originals if component_no is None else originals[2:]
    assert [line["amount"] for line in attempted] == [line["amount"] for line in selected]
    assert [line["component_no"] for line in attempted] == [
        line["component_no"] for line in selected
    ]
    assert {line["id"] for line in attempted}.isdisjoint(line["id"] for line in originals)
    assert all(line["line_no"] > 4 for line in attempted)


def revision_history():
    account, category = uuid4(), uuid4()
    precise = "99999999999999999999.999999999999999999"
    original = SimpleNamespace(
        id=uuid4(),
        kind="posting",
        reverses_journal_id=None,
        transaction_date=date(2026, 3, 1),
        recognition_date=date(2026, 2, 28),
        description="Fictional original history",
        lines=[
            SimpleNamespace(
                id=uuid4(),
                line_no=index,
                component_no=component,
                role=role,
                asset_id=asset,
                account_id=account if role == "account" else None,
                category_id=category if role == "expense" else None,
                amount=amount,
            )
            for index, (component, role, asset, amount) in enumerate(
                [
                    (0, "account", "USD", "-100.00"),
                    (0, "expense", "USD", "100.00"),
                    (1, "account", "ETH", "-" + precise),
                    (1, "expense", "ETH", precise),
                ],
                start=1,
            )
        ],
    )

    def reversal(source):
        result = deepcopy(source)
        result.id, result.kind, result.reverses_journal_id = uuid4(), "reversal", source.id
        for line in result.lines:
            line.id = uuid4()
            line.amount = line.amount[1:] if line.amount.startswith("-") else "-" + line.amount
        return result

    replacement = deepcopy(original)
    replacement.id = uuid4()
    replacement.lines[0].amount, replacement.lines[1].amount = "-120.00", "120.00"
    return [
        SimpleNamespace(version=1, action="create", journals=[original]),
        SimpleNamespace(version=2, action="correct", journals=[reversal(original), replacement]),
        SimpleNamespace(version=3, action="cancel", journals=[reversal(replacement)]),
    ]


def test_revision_restore_probe_preserves_full_precision_under_hostile_decimal_context():
    history = revision_history()
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        verify_revision_history(history, ["create", "correct", "cancel"])


@pytest.mark.parametrize(
    "fault",
    [
        "missing_fee",
        "rounded_fee",
        "wrong_asset",
        "wrong_component",
        "wrong_date",
        "wrong_source",
        "missing_replacement",
        "version_gap",
    ],
)
def test_revision_restore_probe_rejects_partial_or_changed_reversals(fault):
    history = revision_history()
    reversal = history[1].journals[0]
    if fault == "missing_fee":
        reversal.lines = reversal.lines[:2]
    elif fault == "rounded_fee":
        reversal.lines[2].amount = "100000000000000000000.000000000000000000"
    elif fault == "wrong_asset":
        reversal.lines[2].asset_id = "BTC"
    elif fault == "wrong_component":
        reversal.lines[2].component_no = 2
    elif fault == "wrong_date":
        reversal.recognition_date = date(2026, 3, 1)
    elif fault == "wrong_source":
        reversal.reverses_journal_id = uuid4()
    elif fault == "missing_replacement":
        history[1].journals.pop()
    elif fault == "version_gap":
        history[1].version = 3
    with pytest.raises(ArchiveError):
        verify_revision_history(history, ["create", "correct", "cancel"])
