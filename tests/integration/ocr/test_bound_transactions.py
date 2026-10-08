"""Existing financial commands and attachments commit or roll back with their outer session."""

import json
from uuid import uuid4

import pytest
from coinpup_api.files.models import OperationFileLink
from coinpup_api.files.service import DocumentService
from coinpup_api.files.storage import FileStore
from coinpup_api.ledger.models import JournalLine
from coinpup_api.ledger.posting_schemas import ExchangeCreate, TransferCreate
from coinpup_api.ledger.schemas import AccountCreate, AssetUpdate, EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from coinpup_api.ocr.transactions import bind_commands
from sqlalchemy import event, func, select

from tests.integration.files.test_files_service import complete, reserve
from tests.integration.ledger.test_posting_service_database import classified, opening
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ocr.test_ocr_queue import finances
from tests.integration.ocr.test_ocr_schema import rows

pytestmark = pytest.mark.integration


@pytest.fixture
def bound_setup(ledger_setup, tmp_path):
    s = ledger_setup
    s["service"] = DocumentService(s["engine"])
    s["store"] = FileStore(tmp_path / "private", 1024, 5)
    upload, _ = reserve(s)
    s["file"] = complete(s, upload).file_id
    s["second"] = s["structure"].create_account(
        s["owner"],
        s["ledger"],
        AccountCreate(name="Fictional destination", kind="bank", asset_ids=["USD", "EUR"]),
    )
    return s


def state(s):
    with s["engine"].connect() as connection:
        links = connection.execute(select(OperationFileLink.__table__)).mappings().all()
    return finances(s["engine"]), rows(s["engine"]), links


def payload(s, kind):
    if kind == "opening":
        return opening(s, id=uuid4())
    if kind in {"income", "expense"}:
        return classified(s, kind=kind, id=uuid4())
    common = {"id": uuid4(), "transaction_date": "2026-03-01"}
    if kind == "transfer":
        return TransferCreate(
            **common,
            source_account_id=s["account"].id,
            destination_account_id=s["second"].id,
            asset_id="USD",
            amount="12.00",
        )
    return ExchangeCreate(
        **common,
        source_account_id=s["account"].id,
        destination_account_id=s["second"].id,
        source_asset_id="USD",
        source_amount="12.00",
        destination_asset_id="EUR",
        destination_amount="10.00",
    )


@pytest.mark.parametrize("kind", ["opening", "income", "expense", "transfer", "exchange"])
def test_existing_commands_and_link_wait_for_the_outer_commit(bound_setup, kind):
    s = bound_setup
    command = payload(s, kind)
    raw = command.model_dump_json(exclude_unset=True).encode()
    before = state(s)
    with s["structure"]._transaction(s["owner"], write=True) as session:
        transaction = session.get_transaction()
        bound = bind_commands(session, s["owner"], [s["ledger"]], ["USD", "EUR"])
        receipt = bound.post(kind, s["ledger"], command, "fictional-bound", raw_body=raw)
        link = bound.link(s["ledger"], receipt.id, s["file"])
        assert link.operation_id == receipt.id and session.get_transaction() is transaction
        assert state(s) == before  # Separate connections cannot see the uncommitted writes.
    assert s["posting"].get_operation(s["owner"], s["ledger"], receipt.id).id == receipt.id
    assert s["service"].list_operation_files(s["owner"], s["ledger"], receipt.id) == [link]
    with s["engine"].connect() as connection:
        totals = connection.execute(
            select(JournalLine.asset_id, func.sum(JournalLine.amount)).group_by(
                JournalLine.asset_id
            )
        ).all()
        assert totals and all(total == 0 for _, total in totals)
    with pytest.raises(LedgerError, match="transaction is invalid"):
        bound.post(kind, s["ledger"], command, "fictional-bound", raw_body=raw)


@pytest.mark.parametrize("failure", ["after_post", "invalid_file", "after_link"])
def test_outer_failure_rolls_back_posting_receipt_link_and_change_log(bound_setup, failure):
    s = bound_setup
    before = state(s)
    expected = LedgerError if failure == "invalid_file" else RuntimeError
    with pytest.raises(expected) as error:
        with s["structure"]._transaction(s["owner"], write=True) as session:
            bound = bind_commands(session, s["owner"], [s["ledger"]], ["USD"])
            receipt = bound.post("income", s["ledger"], payload(s, "income"), "fictional-rollback")
            if failure == "after_post":
                raise RuntimeError("Fictional confirmation write failed")
            bound.link(s["ledger"], receipt.id, uuid4() if failure == "invalid_file" else s["file"])
            raise RuntimeError("Fictional confirmation receipt write failed")
    if failure == "invalid_file":
        assert error.value.code == "not_found"
    assert state(s) == before


def test_bound_v2_keeps_raw_omissions_and_original_replay_after_archive(bound_setup):
    s = bound_setup
    command = classified(s, kind="income", amount="1.00", id=uuid4())
    original = command.model_dump(mode="json", exclude_unset=True)
    original.pop("description")
    command = type(command).model_validate(original)
    raw = json.dumps(original).encode()
    with s["structure"]._transaction(s["owner"], write=True) as session:
        bound = bind_commands(session, s["owner"], [s["ledger"]], ["USD"])
        receipt = bound.post("income", s["ledger"], command, "fictional-v2", raw_body=raw)
    before = state(s)
    for changes in ({"description": ""}, {"amount": "1.0"}):
        changed = {**original, **changes}
        with pytest.raises(LedgerError) as error:
            with s["structure"]._transaction(s["owner"], write=True) as session:
                bound = bind_commands(session, s["owner"], [s["ledger"]], ["USD"])
                bound.post(
                    "income",
                    s["ledger"],
                    type(command).model_validate(changed),
                    "fictional-v2",
                    raw_body=json.dumps(changed).encode(),
                )
        assert error.value.code == "idempotency_conflict" and state(s) == before
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    before = state(s)
    with s["structure"]._transaction(s["owner"], write=True) as session:
        bound = bind_commands(session, s["owner"], [s["ledger"]], ["USD"])
        assert bound.post("income", s["ledger"], command, "fictional-v2", raw_body=raw) == receipt
    assert state(s) == before


@pytest.mark.parametrize("escape", ["ledger", "asset"])
def test_bound_command_cannot_acquire_an_unlisted_resource(bound_setup, escape):
    s = bound_setup
    other = (
        s["structure"]
        .create_entity(
            s["owner"],
            EntityCreate(kind="personal", name="Fictional unbound ledger", base_asset_id="USD"),
        )
        .ledger.id
    )
    before = state(s)
    with pytest.raises(LedgerError) as error:
        with s["structure"]._transaction(s["owner"], write=True) as session:
            bound = bind_commands(
                session, s["owner"], [s["ledger"]], [] if escape == "asset" else ["USD"]
            )
            bound.post(
                "income",
                other if escape == "ledger" else s["ledger"],
                payload(s, "income"),
                "fictional-escape",
            )
    assert error.value.code == "ocr_transaction_invalid" and state(s) == before


def test_all_scope_locks_precede_any_command_and_use_stable_order(bound_setup):
    s = bound_setup
    other = (
        s["structure"]
        .create_entity(
            s["owner"],
            EntityCreate(kind="personal", name="Fictional second scope", base_asset_id="USD"),
        )
        .ledger.id
    )
    statements = []

    def capture(_conn, _cursor, sql, _parameters, _context, _many):
        statements.append(sql)

    event.listen(s["engine"], "before_cursor_execute", capture)
    try:
        with s["structure"]._transaction(s["owner"], write=True) as session:
            bound = bind_commands(session, s["owner"], [other, s["ledger"]], ["USD", "EUR"])
            locks = [sql for sql in statements if "FOR UPDATE" in sql or "FOR SHARE" in sql]
            assert len(locks) == 3
            assert "pg_advisory_xact_lock" in statements[0]
            assert "ORDER BY ledgers.id FOR UPDATE" in locks[0]
            assert "ORDER BY entities.id FOR UPDATE" in locks[1]
            assert "ORDER BY assets.asset_id FOR SHARE" in locks[2]
            bound.post("income", s["ledger"], payload(s, "income"), "fictional-lock-order")
    finally:
        event.remove(s["engine"], "before_cursor_execute", capture)


@pytest.mark.parametrize("missing", ["owner", "ledger", "asset"])
def test_binding_revalidates_scope_in_the_real_transaction(bound_setup, missing):
    s = bound_setup
    before = state(s)
    with pytest.raises(LedgerError) as error:
        with s["structure"]._transaction(s["owner"], write=True) as session:
            bind_commands(
                session,
                uuid4() if missing == "owner" else s["owner"],
                [s["ledger"], uuid4()] if missing == "ledger" else [s["ledger"]],
                ["fictional-missing-asset"] if missing == "asset" else ["USD"],
            )
    assert error.value.code == "not_found" and state(s) == before
