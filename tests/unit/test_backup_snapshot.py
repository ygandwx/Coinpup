"""Backup comparison must not omit new tables or round exact stored quantities."""

import sys
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from _database_archive import ArchiveError  # noqa: E402
from check_backup_restore import snapshot, verify_sealed_journal  # noqa: E402


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
def test_sealed_probe_always_rolls_back_and_rejects_false_positive_constraints(behavior):
    journal_id = uuid4()
    originals = [
        {"id": uuid4(), "journal_id": journal_id, "line_no": 1, "amount": Decimal("1.00")},
        {"id": uuid4(), "journal_id": journal_id, "line_no": 2, "amount": Decimal("-1.00")},
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
        verify_sealed_journal(engine, journal_id)
    else:
        message = "unexpectedly allowed" if behavior == "allowed" else "unexpected reason"
        with pytest.raises(ArchiveError, match=message):
            verify_sealed_journal(engine, journal_id)
    assert rolled_back == [True]
    assert [line["amount"] for line in attempted] == [line["amount"] for line in originals]
    assert {line["id"] for line in attempted}.isdisjoint(line["id"] for line in originals)
    assert all(line["line_no"] > 2 for line in attempted)
