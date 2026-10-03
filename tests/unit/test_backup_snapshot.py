"""Backup comparison must not omit new tables or round exact stored quantities."""

import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from check_backup_restore import snapshot  # noqa: E402


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
