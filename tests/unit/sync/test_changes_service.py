"""Change cursors retain exact text, owner scope and the last delivered sequence."""

from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from coinpup_api.ledger.service import LedgerError
from coinpup_api.models import Administrator
from coinpup_api.sync.schemas import MAX_CURSOR, Cursor
from coinpup_api.sync.service import ChangeService
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.dialects import postgresql

OWNER = UUID("00000000-0000-4000-8000-000000000001")
BAD_CURSORS = [
    "",
    "00",
    "01",
    "-1",
    "+1",
    " 1",
    "1 ",
    "1\n",
    "1.0",
    "1e2",
    "NaN",
    "Infinity",
    "2026-01-01",
    "1 OR 1=1",
    "١",
    str(MAX_CURSOR + 1),
    "9" * 100,
    1,
    1.0,
    True,
    None,
    b"1",
]


def row(seq, **changes):
    fields = {
        "seq": seq,
        "owner_id": OWNER,
        "ledger_id": None,
        "entity_type": "assets",
        "entity_id": "ETH:fictional-token:完整标识",
        "entity_version": 1,
        "change_kind": "upsert",
        "changed_at": datetime(2026, 10, 4, tzinfo=UTC),
    }
    fields.update(changes)
    return SimpleNamespace(**fields)


def service_with_rows(monkeypatch, rows):
    service = ChangeService(None)
    transactions, statements = [], []

    def scalars(statement):
        statements.append(statement)
        return SimpleNamespace(all=lambda: rows)

    @contextmanager
    def transaction(owner, **options):
        transactions.append((owner, options))
        yield SimpleNamespace(scalars=scalars)

    monkeypatch.setattr(service, "_transaction", transaction)
    return service, transactions, statements


@pytest.mark.parametrize("cursor", BAD_CURSORS)
def test_cursor_schema_and_service_reject_invalid_values_before_storage(cursor):
    with pytest.raises(ValidationError):
        TypeAdapter(Cursor).validate_python(cursor)
    with pytest.raises(LedgerError) as error:
        ChangeService(None).list_changes(OWNER, after=cursor)
    assert (error.value.code, error.value.status) == ("invalid_cursor", 422)


@pytest.mark.parametrize("limit", [0, 201, -1, True, 1.0, "1"])
def test_change_page_limit_is_bounded_before_storage(limit):
    with pytest.raises(LedgerError) as error:
        ChangeService(None).list_changes(OWNER, limit=limit)
    assert (error.value.code, error.value.status) == ("invalid_pagination", 422)


@pytest.mark.parametrize("cursor", ["0", "9007199254740993", str(MAX_CURSOR)])
def test_empty_page_retains_input_cursor_and_read_only_owner_query(monkeypatch, cursor):
    service, transactions, statements = service_with_rows(monkeypatch, [])
    page = service.list_changes(OWNER, after=cursor, limit=2)
    assert page.model_dump(mode="json") == {"changes": [], "next_cursor": cursor}
    assert transactions == [(OWNER, {"read_only": True})]
    assert len(statements) == 1
    compiled = statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "change_log.owner_id =" in sql and "change_log.seq >" in sql
    assert "ORDER BY change_log.seq ASC" in sql and "LIMIT" in sql
    assert set(compiled.params.values()) == {OWNER, int(cursor), 2}
    assert str(OWNER) not in sql and "max(" not in sql.lower()


def test_cursor_advances_only_to_last_delivered_row_and_sequence_is_never_number(monkeypatch):
    records = [row(9007199254740993), row(MAX_CURSOR)]
    service, _, _ = service_with_rows(monkeypatch, records)
    page = service.list_changes(OWNER, after="42")
    data = page.model_dump(mode="json")
    assert data["next_cursor"] == str(MAX_CURSOR)
    assert [item["seq"] for item in data["changes"]] == ["9007199254740993", str(MAX_CURSOR)]
    assert data["changes"][0] == {
        "seq": "9007199254740993",
        "owner_id": str(OWNER),
        "ledger_id": None,
        "entity_type": "assets",
        "entity_id": "ETH:fictional-token:完整标识",
        "entity_version": 1,
        "change_kind": "upsert",
        "changed_at": "2026-10-04T00:00:00Z",
    }


@pytest.mark.parametrize(
    "corruption", [{"seq": 0}, {"entity_version": 0}, {"change_kind": "delete"}]
)
def test_corrupt_notification_is_a_stable_integrity_error(monkeypatch, corruption):
    record = row(1, entity_id="fictional-private-notification")
    for name, value in corruption.items():
        setattr(record, name, value)
    service, _, _ = service_with_rows(monkeypatch, [record])
    with pytest.raises(LedgerError) as error:
        service.list_changes(OWNER)
    assert (error.value.code, error.value.status) == ("ledger_integrity", 503)
    assert "fictional-private-notification" not in str(error.value)


def test_unknown_owner_is_checked_by_inherited_transaction_before_reading(monkeypatch):
    calls = []
    owner = uuid4()

    class Session:
        def __init__(self, engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def begin(self):
            return nullcontext()

        def execute(self, statement):
            calls.append(str(statement))

        def get(self, model, identifier):
            assert model is Administrator and identifier == owner
            calls.append("owner lookup")
            return None

        def scalars(self, statement):
            pytest.fail("Unknown owner reached change log")

    monkeypatch.setattr("coinpup_api.ledger.service.Session", Session)
    with pytest.raises(LedgerError) as error:
        ChangeService(object()).list_changes(owner)
    assert (error.value.code, error.value.status) == ("not_found", 404)
    assert calls == ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY", "owner lookup"]
