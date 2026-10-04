"""Real source triggers, decimal cursors and protected notification history."""

import runpy
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from coinpup_api.files.schemas import FileUpdate, LinkUpdate
from coinpup_api.files.service import DocumentService
from coinpup_api.files.storage import FileStore
from coinpup_api.ledger.models import AccountAsset, Category, Ledger
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.posting_schemas import (
    CancellationCreate,
    CorrectionCreate,
    ExpenseCreate,
    OpeningCreate,
)
from coinpup_api.ledger.schemas import (
    AccountCreate,
    AccountUpdate,
    AssetCreate,
    AssetUpdate,
    CategoryCreate,
    CategoryUpdate,
    EntityCreate,
    EntityUpdate,
)
from coinpup_api.ledger.service import LedgerError, LedgerService
from coinpup_api.sync.models import ChangeLog
from coinpup_api.sync.service import ChangeService
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]
TABLES = {
    "entities",
    "ledgers",
    "accounts",
    "account_assets",
    "categories",
    "assets",
    "financial_operations",
    "stored_files",
    "operation_file_links",
}


def log_rows(engine):
    with engine.connect() as connection:
        return (
            connection.execute(select(ChangeLog.__table__).order_by(ChangeLog.seq)).mappings().all()
        )


def sequence_name(connection):
    return connection.scalar(text("SELECT pg_get_serial_sequence('public.change_log', 'seq')"))


@pytest.fixture
def change_setup(structure_database):
    engine, owner = structure_database
    structure, posting = LedgerService(engine), PostingService(engine)
    entity = structure.create_entity(
        owner, EntityCreate(kind="personal", name="Fictional change ledger", base_asset_id="USD")
    )
    account = structure.create_account(
        owner,
        entity.ledger.id,
        AccountCreate(name="Fictional change bank", kind="bank", asset_ids=["USD"]),
    )
    category = structure.create_category(
        owner, entity.ledger.id, CategoryCreate(name="Fictional expense", kind="expense")
    )
    return {
        "engine": engine,
        "owner": owner,
        "structure": structure,
        "posting": posting,
        "entity": entity,
        "ledger": entity.ledger.id,
        "account": account,
        "category": category,
    }


def test_all_nine_sources_insert_update_and_archive_restore_cancel(change_setup, tmp_path):
    s = change_setup
    engine, owner, ledger, structure = s["engine"], s["owner"], s["ledger"], s["structure"]
    token = structure.create_asset(
        owner,
        AssetCreate(
            code="USDC",
            kind="token",
            scale=6,
            network="fictional-change-chain",
            token_reference="FictionalToken",
        ),
    )
    account = structure.update_account(
        owner,
        ledger,
        s["account"].id,
        AccountUpdate(expected_version=1, asset_ids=["USD", token.asset_id]),
    )
    opening = s["posting"].post_opening(
        owner,
        ledger,
        OpeningCreate(
            account_id=account.id, asset_id="USD", amount="100", transaction_date="2026-01-01"
        ),
        "fictional-changes-opening",
    )
    files = runpy.run_path(str(ROOT / "tests/integration/files/test_files_service.py"))
    file_setup = s | {
        "service": DocumentService(engine, 1024),
        "store": FileStore(tmp_path / "private", 1024, 5),
    }
    before_pending = log_rows(engine)
    manifest, pending = files["reserve"](file_setup, operation=opening.id)
    assert log_rows(engine) == before_pending  # Pending uploads are not published objects.
    receipt = files["complete"](file_setup, pending)
    inserts = log_rows(engine)
    assert {row["entity_type"] for row in inserts} == TABLES
    assert all(row["owner_id"] == owner for row in inserts)
    assert all(
        row["ledger_id"] == ledger
        for row in inserts
        if row["entity_type"] not in {"entities", "assets"}
    )
    assert all(
        row["ledger_id"] is None for row in inserts if row["entity_type"] in {"entities", "assets"}
    )
    link_id = f"{account.id}:{token.asset_id}"
    association = next(row for row in inserts if row["entity_id"] == link_id)
    assert association["entity_type"] == "account_assets"
    assert association["entity_version"] > 0 and link_id.endswith(token.asset_id)

    start = inserts[-1]["seq"]
    structure.update_entity(
        owner, s["entity"].id, EntityUpdate(expected_version=1, name="Fictional renamed entity")
    )
    # Ledger has no update endpoint; a real tracked SQL update exercises its database contract.
    with structure._transaction(owner, write=True) as session:
        session.execute(update(Ledger).where(Ledger.id == ledger).values(version=2))
    account = structure.update_account(
        owner, ledger, account.id, AccountUpdate(expected_version=account.version, archived=True)
    )
    account = structure.update_account(
        owner, ledger, account.id, AccountUpdate(expected_version=account.version, archived=False)
    )
    with structure._transaction(owner, write=True) as session:
        session.execute(
            update(AccountAsset)
            .where(AccountAsset.account_id == account.id, AccountAsset.asset_id == token.asset_id)
            .values(enabled=False)
        )
    with structure._transaction(owner, write=True) as session:
        session.execute(
            update(AccountAsset)
            .where(AccountAsset.account_id == account.id, AccountAsset.asset_id == token.asset_id)
            .values(enabled=True)
        )
    category = structure.update_category(
        owner, ledger, s["category"].id, CategoryUpdate(expected_version=1, archived=True)
    )
    structure.update_category(
        owner,
        ledger,
        category.id,
        CategoryUpdate(expected_version=category.version, archived=False),
    )
    asset = structure.update_asset(
        owner, token.asset_id, AssetUpdate(expected_version=1, enabled=False)
    )
    structure.update_asset(
        owner, token.asset_id, AssetUpdate(expected_version=asset.version, enabled=True)
    )
    document = file_setup["service"].update_file(
        owner, ledger, receipt.file_id, FileUpdate(expected_version=1, archived=True)
    )
    file_setup["service"].update_file(
        owner, ledger, document.id, FileUpdate(expected_version=document.version, archived=False)
    )
    link = file_setup["service"].update_link(
        owner, ledger, opening.id, receipt.file_id, LinkUpdate(expected_version=1, archived=True)
    )
    file_setup["service"].update_link(
        owner,
        ledger,
        opening.id,
        receipt.file_id,
        LinkUpdate(expected_version=link.version, archived=False),
    )
    cancellation = s["posting"].cancel_operation(
        owner,
        ledger,
        opening.id,
        CancellationCreate(expected_version=1, reason="Fictional cancelled opening"),
        "fictional-changes-cancel",
    )
    changed = [row for row in log_rows(engine) if row["seq"] > start]
    assert {row["entity_type"] for row in changed} == TABLES
    for table in {
        "accounts",
        "account_assets",
        "categories",
        "assets",
        "stored_files",
        "operation_file_links",
    }:
        assert [row["change_kind"] for row in changed if row["entity_type"] == table] == [
            "archive",
            "restore",
        ]
    assert [row["entity_version"] for row in changed if row["entity_type"] == "account_assets"] == [
        account.version,
        account.version,
    ]  # Association updates do not invent a parent version.
    financial = [row for row in changed if row["entity_type"] == "financial_operations"]
    assert len(financial) == 1 and financial[0]["change_kind"] == "cancel"
    assert financial[0]["entity_version"] == cancellation.version
    before_replay = log_rows(engine)
    assert file_setup["service"].reserve_upload(owner, ledger, manifest).response == receipt
    assert files["complete"](file_setup, pending) == receipt
    assert (
        s["posting"].cancel_operation(
            owner,
            ledger,
            opening.id,
            CancellationCreate(expected_version=1, reason="Fictional cancelled opening"),
            "fictional-changes-cancel",
        )
        == cancellation
    )
    assert log_rows(engine) == before_replay


def test_failed_financial_command_and_rolled_back_insert_leave_no_events(change_setup):
    s = change_setup
    engine, owner, ledger = s["engine"], s["owner"], s["ledger"]
    before = log_rows(engine)
    with pytest.raises(LedgerError) as failure:
        s["posting"].post_expense(
            owner,
            ledger,
            ExpenseCreate(
                account_id=s["account"].id,
                asset_id="USD",
                amount="5",
                transaction_date="2026-01-01",
                recognition_date="2026-01-01",
                splits=[{"category_id": uuid4(), "amount": "5"}],
            ),
            "fictional-invalid-change-expense",
        )
    assert failure.value.code == "not_found"
    assert log_rows(engine) == before
    financial = runpy.run_path(str(ROOT / "tests/integration/ledger/test_revision_constraints.py"))
    immutable = runpy.run_path(str(ROOT / "tests/integration/ledger/legacy_v1_receipts.py"))
    original_facts = immutable["financial_snapshot"](engine)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(
                insert(Category).values(
                    id=uuid4(),
                    ledger_id=ledger,
                    name="Fictional rolled-back category",
                    kind="expense",
                )
            )
            abandoned = connection.scalar(select(func.max(ChangeLog.seq)))
            assert abandoned > before[-1]["seq"]
            operation, _journal = financial["_initial"](
                connection,
                {
                    "owner": owner,
                    "ledger": ledger,
                    "accounts": [s["account"].id],
                    "categories": [s["category"].id],
                },
            )
            assert (
                connection.scalar(
                    select(ChangeLog.seq).where(ChangeLog.entity_id == str(operation))
                )
                > abandoned
            )
            connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        finally:
            transaction.rollback()
    assert log_rows(engine) == before
    assert immutable["financial_snapshot"](engine) == original_facts
    category = s["structure"].create_category(
        owner, ledger, CategoryCreate(name="Fictional committed after gap", kind="expense")
    )
    page = ChangeService(engine).list_changes(owner, after=str(before[-1]["seq"]))
    assert len(page.changes) == 1 and page.changes[0].entity_id == str(category.id)
    assert int(page.next_cursor) > abandoned
    empty = ChangeService(engine).list_changes(owner, after=page.next_cursor)
    assert empty.changes == [] and empty.next_cursor == page.next_cursor


def test_financial_revision_events_and_all_original_receipts_replay_without_new_events(
    change_setup,
):
    s = change_setup
    body = dict(
        account_id=s["account"].id,
        asset_id="USD",
        amount="5",
        transaction_date="2026-01-01",
        recognition_date="2026-01-01",
        splits=[{"category_id": s["category"].id, "amount": "5"}],
    )
    original = s["posting"].post_expense(
        s["owner"], s["ledger"], ExpenseCreate(**body), "change-expense"
    )
    correction = CorrectionCreate(
        expected_version=1,
        reason="Fictional corrected expense",
        replacement=body
        | {
            "kind": "expense",
            "amount": "7",
            "splits": [{"category_id": s["category"].id, "amount": "7"}],
        },
    )
    corrected = s["posting"].correct_operation(
        s["owner"], s["ledger"], original.id, correction, "change-correction"
    )
    cancel = CancellationCreate(expected_version=2, reason="Fictional cancelled correction")
    cancelled = s["posting"].cancel_operation(
        s["owner"], s["ledger"], original.id, cancel, "change-cancellation"
    )
    before = log_rows(s["engine"])
    events = [row for row in before if row["entity_id"] == str(original.id)]
    assert [(row["entity_version"], row["change_kind"]) for row in events] == [
        (1, "upsert"),
        (2, "upsert"),
        (3, "cancel"),
    ]
    assert (
        s["posting"].post_expense(s["owner"], s["ledger"], ExpenseCreate(**body), "change-expense")
        == original
    )
    assert (
        s["posting"].correct_operation(
            s["owner"], s["ledger"], original.id, correction, "change-correction"
        )
        == corrected
    )
    assert (
        s["posting"].cancel_operation(
            s["owner"], s["ledger"], original.id, cancel, "change-cancellation"
        )
        == cancelled
    )
    assert log_rows(s["engine"]) == before


@pytest.mark.parametrize(
    "operation,constraint",
    [
        ("update", "ck_change_log_immutable"),
        ("delete", "ck_change_log_immutable"),
        ("insert", "ck_change_log_source"),
    ],
)
def test_notification_history_rejects_mutation_and_direct_injection(
    change_setup, operation, constraint
):
    engine = change_setup["engine"]
    before = log_rows(engine)
    row = before[0]
    with pytest.raises(IntegrityError) as failure:
        with engine.begin() as connection:
            if operation == "update":
                connection.execute(
                    update(ChangeLog).where(ChangeLog.seq == row["seq"]).values(entity_version=99)
                )
            elif operation == "delete":
                connection.exec_driver_sql("DELETE FROM change_log")
            else:
                connection.execute(
                    insert(ChangeLog).values(
                        **{key: value for key, value in row.items() if key != "seq"}
                    )
                )
    assert failure.value.orig.sqlstate == "23514"
    assert failure.value.orig.diag.constraint_name == constraint
    assert log_rows(engine) == before


def test_http_pages_exact_huge_cursors_owner_isolation_and_default_limits(
    change_setup, authenticated_client
):
    s = change_setup
    client, engine, owner = authenticated_client
    assert (engine, owner) == (s["engine"], s["owner"])
    other = s["structure"].create_entity(
        owner,
        EntityCreate(kind="personal", name="Fictional second change ledger", base_asset_id="USD"),
    )
    prior = str(log_rows(engine)[-1]["seq"])
    with engine.begin() as connection:
        name = sequence_name(connection)
        # This disposable test advances the real identity; it never injects log rows or rewinds it.
        quoted = ".".join(
            connection.dialect.identifier_preparer.quote(part) for part in name.split(".")
        )
        previous = connection.exec_driver_sql(f"SELECT last_value FROM {quoted}").scalar()
        huge = max(2**53 + 17, previous + 17)
        connection.exec_driver_sql(f"ALTER SEQUENCE {quoted} RESTART WITH {huge}")
    with s["structure"]._transaction(owner, write=True) as session:
        session.execute(
            insert(Category),
            [
                {
                    "id": uuid4(),
                    "ledger_id": s["ledger"] if index % 2 else other.ledger.id,
                    "name": f"Fictional paged category {index}",
                    "kind": "expense",
                }
                for index in range(205)
            ],
        )
    default = client.get("/api/v1/changes", params={"after": prior})
    assert default.status_code == 200, default.text
    body = default.json()
    assert len(body["changes"]) == 100
    assert body["changes"][0]["seq"] == str(huge)
    assert body["next_cursor"] == str(huge + 99)
    fields = {
        "seq",
        "owner_id",
        "ledger_id",
        "entity_type",
        "entity_id",
        "entity_version",
        "change_kind",
        "changed_at",
    }
    assert all(set(row) == fields and row["owner_id"] == str(owner) for row in body["changes"])
    assert {row["ledger_id"] for row in body["changes"]} == {str(s["ledger"]), str(other.ledger.id)}
    rest = client.get("/api/v1/changes", params={"after": body["next_cursor"], "limit": 200})
    assert rest.status_code == 200
    assert len(rest.json()["changes"]) == 105
    assert rest.json()["next_cursor"] == str(huge + 204)
    full = client.get("/api/v1/changes", params={"after": prior, "limit": 200}).json()
    assert len(full["changes"]) == 200 and full["next_cursor"] == str(huge + 199)
    delivered = body["changes"] + rest.json()["changes"]
    assert [item["seq"] for item in delivered] == [str(huge + index) for index in range(205)]
    empty = client.get("/api/v1/changes", params={"after": rest.json()["next_cursor"]})
    assert empty.json() == {"changes": [], "next_cursor": str(huge + 204)}
    assert client.get("/api/v1/changes", params={"limit": 201}).status_code == 422
    with pytest.raises(LedgerError) as missing:
        ChangeService(engine).list_changes(uuid4())
    assert missing.value.code == "not_found"
    client.cookies.clear()
    assert client.get("/api/v1/changes").status_code == 401


def test_real_chain_downgrade_refuses_published_changes_without_altering_history(change_setup):
    engine = change_setup["engine"]
    before = log_rows(engine)
    with engine.connect() as connection:
        head = connection.scalar(text("SELECT version_num FROM alembic_version"))
        triggers = connection.execute(
            text("SELECT tgname FROM pg_trigger WHERE tgname LIKE :pattern ORDER BY tgname"),
            {"pattern": "trg_%_change_log"},
        ).all()
    with pytest.raises(IntegrityError) as rejected:
        command.downgrade(Config(str(ROOT / "alembic.ini")), "20261004_0011")
    assert rejected.value.orig.sqlstate == "23514"
    assert rejected.value.orig.diag.constraint_name == "ck_change_log_downgrade"
    assert log_rows(engine) == before
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == head
        assert (
            connection.execute(
                text("SELECT tgname FROM pg_trigger WHERE tgname LIKE :pattern ORDER BY tgname"),
                {"pattern": "trg_%_change_log"},
            ).all()
            == triggers
        )
