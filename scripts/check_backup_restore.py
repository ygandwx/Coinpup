"""Opt-in CI exercise. Both URLs must point to disposable test databases; never drops a DB."""

import os
import secrets
import sys
import tempfile
from datetime import date
from pathlib import Path
from uuid import uuid4

import psycopg
from _database_archive import ArchiveError, read_target, require_posix
from backup_database import backup_database
from psycopg import sql
from restore_database import restore_database


def snapshot(target):
    """Compare every application table without decoding precise JSON numbers as floats."""
    with target.connect() as connection:
        connection.execute("SET LOCAL TIME ZONE 'UTC'")
        tables = connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
            "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend dependency "
            "WHERE dependency.classid = 'pg_catalog.pg_class'::regclass "
            "AND dependency.objid = to_regclass(format('%I.%I', table_schema, table_name)) "
            "AND dependency.deptype = 'e') ORDER BY table_name"
        ).fetchall()
        return {
            name: connection.execute(
                sql.SQL(
                    "SELECT row_to_json(t)::text FROM {} t ORDER BY row_to_json(t)::text"
                ).format(sql.Identifier("public", name))
            ).fetchall()
            for (name,) in tables
        }


def create_structure_fixture(engine, owner_id):
    """Seed only explicitly fictional data through the same commands used by the API."""
    from coinpup_api.ledger.schemas import (
        AccountCreate,
        AccountUpdate,
        AssetCreate,
        CategoryCreate,
        CategoryUpdate,
        EntityCreate,
    )
    from coinpup_api.ledger.service import LedgerService

    service = LedgerService(engine)
    yen = service.create_asset(owner_id, AssetCreate(code="JPY", kind="fiat", scale=0))
    token = service.create_asset(
        owner_id,
        AssetCreate(
            code="USDC",
            kind="token",
            scale=6,
            network="fictional-backup-chain",
            token_reference="FictionalBackupToken",
        ),
    )
    personal = service.create_entity(
        owner_id,
        EntityCreate(
            kind="personal",
            name="Fictional backup personal",
            base_asset_id="USD",
            template_key="business_default",
            locale="en",
        ),
    )
    company = service.create_entity(
        owner_id,
        EntityCreate(
            kind="company",
            name="Fictional backup company",
            country_code="US",
            region_code="NM",
            company_type="llc",
            base_asset_id="USD",
            template_key="business_default",
            locale="en",
            details={"fixture_note": "Synthetic CI record; not a registered company"},
        ),
    )
    personal_account = service.create_account(
        owner_id,
        personal.ledger.id,
        AccountCreate(
            name="Fictional personal Wise", kind="wise", asset_ids=["USD", "EUR", yen.asset_id]
        ),
    )
    company_account = service.create_account(
        owner_id,
        company.ledger.id,
        AccountCreate(
            name="Fictional company account",
            kind="bank",
            asset_ids=["USD", "ETH", yen.asset_id, token.asset_id],
        ),
    )
    old_account = service.create_account(
        owner_id,
        company.ledger.id,
        AccountCreate(name="Fictional archived cash", kind="cash", asset_ids=[yen.asset_id]),
    )
    archived = service.update_account(
        owner_id,
        company.ledger.id,
        old_account.id,
        AccountUpdate(expected_version=old_account.version, archived=True),
    )
    original_categories = service.list_categories(owner_id, personal.ledger.id)
    parent = next(
        category
        for category in service.list_categories(owner_id, company.ledger.id)
        if category.kind == "expense"
    )
    renamed = service.update_category(
        owner_id,
        company.ledger.id,
        parent.id,
        CategoryUpdate(expected_version=parent.version, name="Fictional renamed expense"),
    )
    child = service.create_category(
        owner_id,
        company.ledger.id,
        CategoryCreate(name="Fictional child expense", kind="expense", parent_id=parent.id),
    )
    if (
        archived.version != old_account.version + 1
        or not archived.archived
        or renamed.version != parent.version + 1
        or child.parent_id != parent.id
        or service.list_categories(owner_id, personal.ledger.id) != original_categories
    ):
        raise ArchiveError("CI structure fixture did not preserve version or category isolation.")
    return {
        "personal": personal,
        "company": company,
        "personal_account": personal_account,
        "company_account": company_account,
        "archived_account": archived,
        "token_asset_id": token.asset_id,
    }


def structure_state(engine, owner_id):
    """Resolve restored references through business reads, including archived history."""
    from coinpup_api.ledger.service import LedgerService

    service = LedgerService(engine)

    def records(items, key="id"):
        return sorted((item.model_dump(mode="json") for item in items), key=lambda item: item[key])

    assets = service.list_assets(owner_id, include_disabled=True)
    entities = service.list_entities(owner_id, include_archived=True)
    asset_ids = {asset.asset_id for asset in assets}
    ledgers = {}
    for entity in entities:
        ledger = service.get_ledger(owner_id, entity.ledger.id)
        accounts = service.list_accounts(owner_id, ledger.id, include_archived=True)
        active_accounts = service.list_accounts(owner_id, ledger.id)
        categories = service.list_categories(owner_id, ledger.id, include_archived=True)
        category_ids = {category.id for category in categories}
        if (
            ledger != entity.ledger
            or ledger.entity_id != entity.id
            or ledger.base_asset_id not in asset_ids
            or any(
                account.ledger_id != ledger.id or not set(account.asset_ids) <= asset_ids
                for account in accounts
            )
            or {account.id for account in active_accounts}
            != {account.id for account in accounts if not account.archived}
            or any(
                category.ledger_id != ledger.id
                or (category.parent_id is not None and category.parent_id not in category_ids)
                for category in categories
            )
        ):
            raise ArchiveError("Original or restored business references could not be resolved.")
        ledgers[str(ledger.id)] = {
            "ledger": ledger.model_dump(mode="json"),
            "accounts": records(accounts),
            "active_accounts": records(active_accounts),
            "categories": records(categories),
        }
    if len(entities) != 2 or len(assets) != 10 or len(ledgers) != 2:
        raise ArchiveError("Original or restored structure fixture has unexpected record counts.")
    if sum(len(ledger["accounts"]) for ledger in ledgers.values()) != 3:
        raise ArchiveError("Original or restored multi-asset account fixture is incomplete.")
    return {
        "assets": records(assets, "asset_id"),
        "entities": records(entities),
        "ledgers": ledgers,
    }


def create_financial_fixture(engine, owner_id, structure):
    """Exercise exact quantities and separate recognition/payment dates before backup."""
    from coinpup_api.ledger.posting import PostingService
    from coinpup_api.ledger.posting_schemas import ExpenseCreate, IncomeCreate, OpeningCreate
    from coinpup_api.ledger.schemas import AccountUpdate
    from coinpup_api.ledger.service import LedgerService

    service = PostingService(engine)
    personal = structure["personal"]
    company = structure["company"]
    personal_account = structure["personal_account"]
    company_account = structure["company_account"]
    for ledger_id, account_id, asset_id, quantity in (
        (personal.ledger.id, personal_account.id, "USD", "100.00"),
        (personal.ledger.id, personal_account.id, "EUR", "80.00"),
        (company.ledger.id, company_account.id, "ETH", "1.000000000000000001"),
        (company.ledger.id, company_account.id, structure["token_asset_id"], "12.345678"),
        (company.ledger.id, company_account.id, "JPY", "123"),
    ):
        service.post_opening(
            owner_id,
            ledger_id,
            OpeningCreate(
                account_id=account_id,
                asset_id=asset_id,
                amount=quantity,
                transaction_date=date(2026, 1, 1),
                description="Fictional backup opening",
            ),
            str(uuid4()),
        )
    categories = LedgerService(engine).list_categories(owner_id, personal.ledger.id)
    expenses = [category for category in categories if category.kind == "expense"]
    income = next(category for category in categories if category.kind == "income")
    request = ExpenseCreate(
        account_id=personal_account.id,
        asset_id="USD",
        amount="25.00",
        transaction_date=date(2026, 2, 1),
        recognition_date=date(2026, 1, 15),
        description="Fictional split expense paid after recognition",
        splits=[
            {"category_id": expenses[0].id, "amount": "10.00"},
            {"category_id": expenses[1].id, "amount": "15.00"},
        ],
    )
    key = str(uuid4())
    receipt = service.post_expense(owner_id, personal.ledger.id, request, key)
    service.post_income(
        owner_id,
        personal.ledger.id,
        IncomeCreate(
            account_id=personal_account.id,
            asset_id="EUR",
            amount="10.00",
            transaction_date=date(2026, 2, 2),
            recognition_date=date(2026, 1, 31),
            description="Fictional backup income",
            splits=[{"category_id": income.id, "amount": "10.00"}],
        ),
        str(uuid4()),
    )
    # History and accepted-command replay remain usable after the account is archived.
    LedgerService(engine).update_account(
        owner_id,
        personal.ledger.id,
        personal_account.id,
        AccountUpdate(expected_version=personal_account.version, archived=True),
    )
    expected_amounts = {
        (str(personal_account.id), "USD"): "75.00",
        (str(personal_account.id), "EUR"): "90.00",
        (str(company_account.id), "ETH"): "1.000000000000000001",
        (str(company_account.id), structure["token_asset_id"]): "12.345678",
        (str(company_account.id), "JPY"): "123",
    }
    state = financial_state(engine, owner_id, structure)
    amounts = {
        (balance["account_id"], balance["asset_id"]): balance["amount"]
        for balances in state.values()
        for balance in balances
    }
    if any(amounts.get(key) != amount for key, amount in expected_amounts.items()):
        raise ArchiveError("CI financial fixture balances do not match exact expected quantities.")
    if any(
        not balance["account_archived"]
        for balance in state[str(personal.ledger.id)]
        if balance["account_id"] == str(personal_account.id)
    ):
        raise ArchiveError("Archived account history lost its archive status.")
    return {
        "ledger_id": personal.ledger.id,
        "request": request,
        "key": key,
        "receipt": receipt,
        "balances": state,
    }


def financial_state(engine, owner_id, structure):
    from coinpup_api.ledger.posting import PostingService

    service = PostingService(engine)
    return {
        str(entity.ledger.id): sorted(
            (
                balance.model_dump(mode="json")
                for balance in service.balances(owner_id, entity.ledger.id)
            ),
            key=lambda balance: (balance["account_id"], balance["asset_id"]),
        )
        for entity in (structure["personal"], structure["company"])
    }


def verify_sealed_journal(engine, journal_id):
    """Try appending a balanced copy, then roll back regardless of the outcome."""
    from coinpup_api.ledger.models import JournalLine
    from sqlalchemy import insert, select, text
    from sqlalchemy.exc import IntegrityError

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            originals = (
                connection.execute(
                    select(JournalLine.__table__).where(JournalLine.journal_id == journal_id)
                )
                .mappings()
                .all()
            )
            if len(originals) < 2:
                raise ArchiveError("CI sealed journal fixture has too few lines.")
            highest_line = max(row["line_no"] for row in originals)
            copies = [
                dict(row, id=uuid4(), line_no=highest_line + index)
                for index, row in enumerate(originals, start=1)
            ]
            try:
                connection.execute(insert(JournalLine), copies)
                # Trigger all deferred checks without committing the destructive probe.
                connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            except IntegrityError as error:
                if (
                    getattr(error.orig, "sqlstate", None) != "23514"
                    or getattr(getattr(error.orig, "diag", None), "constraint_name", None)
                    != "ck_journal_sealed"
                ):
                    raise ArchiveError(
                        "Restored journal rejected append for an unexpected reason."
                    ) from None
            else:
                raise ArchiveError("Restored sealed journal unexpectedly allowed new lines.")
        finally:
            transaction.rollback()


def check_backup_restore():
    require_posix()
    if os.environ.get("COINPUP_RUN_BACKUP_TESTS") != "1":
        raise ArchiveError("Set COINPUP_RUN_BACKUP_TESTS=1 only for disposable CI databases.")
    source = read_target("COINPUP_BACKUP_TEST_SOURCE_URL")
    target = read_target("COINPUP_BACKUP_TEST_TARGET_URL")
    if (
        target.database != "coinpup_restore_test"
        or source.database == target.database
        or (source.host, source.port, source.user) != (target.host, target.port, target.user)
    ):
        raise ArchiveError(
            "CI target must be coinpup_restore_test on the same test server and user."
        )
    # Refuse existing target and existing identity: never overwrite or reset either.
    with source.connect(autocommit=True) as connection:
        if connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)", (target.database,)
        ).fetchone()[0]:
            raise ArchiveError(
                "CI restore database already exists; use a fresh PostgreSQL service."
            )
        if connection.execute("SELECT EXISTS (SELECT 1 FROM administrators)").fetchone()[0]:
            raise ArchiveError(
                "CI source already has an administrator; use a fresh migrated source."
            )
        connection.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(target.database))
        )

    from coinpup_api.admin import create_admin
    from coinpup_api.auth import AuthService
    from coinpup_api.config import Settings
    from sqlalchemy import create_engine

    source_url = os.environ["COINPUP_BACKUP_TEST_SOURCE_URL"]
    target_url = os.environ["COINPUP_BACKUP_TEST_TARGET_URL"]
    # Accept plain PostgreSQL URLs in CLI configuration, consistently use psycopg in SQLAlchemy.
    source_url = source_url.replace("postgresql://", "postgresql+psycopg://", 1)
    target_url = target_url.replace("postgresql://", "postgresql+psycopg://", 1)
    settings = Settings(environment="test", database_url=source_url, _env_file=None)
    source_engine = create_engine(source_url, hide_parameters=True)
    target_engine = create_engine(target_url, hide_parameters=True)
    try:
        password = secrets.token_urlsafe(32)
        administrator_id = create_admin(source_engine, "backup-ci-fixture", password)
        identity, token = AuthService(settings, source_engine).login("backup-ci-fixture", password)
        if identity.id != administrator_id:
            raise ArchiveError("CI fixture login did not match its administrator.")
        structure = create_structure_fixture(source_engine, administrator_id)
        financial = create_financial_fixture(source_engine, administrator_id, structure)
        expected_structure = structure_state(source_engine, administrator_id)
        source_engine.dispose()
        before = snapshot(source)
        required_tables = {
            "assets",
            "entities",
            "ledgers",
            "accounts",
            "account_assets",
            "categories",
            "financial_operations",
            "journals",
            "journal_lines",
            "opening_positions",
            "command_receipts",
        }
        if any(not before.get(table) for table in required_tables):
            raise ArchiveError(
                "CI business structure tables must contain fixture rows before backup."
            )
        with tempfile.TemporaryDirectory(prefix="coinpup-backup-check-") as temporary:
            archive = Path(temporary) / "backup"
            backup_database(source, archive)
            restore_database(target, archive, target.database)
            try:
                restore_database(target, archive, target.database)
            except ArchiveError as error:
                if "not empty" not in str(error):
                    raise
            else:
                raise ArchiveError("Second restore should have refused the nonempty target.")
        if before != snapshot(source) or before != snapshot(target):
            raise ArchiveError("Source/restore application rows or migration revision differ.")
        for engine in (source_engine, target_engine):
            if AuthService(settings, engine).get_session(token).id != administrator_id:
                raise ArchiveError("Restored or original session is not usable.")
            if structure_state(engine, administrator_id) != expected_structure:
                raise ArchiveError(
                    "Restored or original business structure differs from its fixture."
                )
            if financial_state(engine, administrator_id, structure) != financial["balances"]:
                raise ArchiveError("Restored or original exact account balances differ.")
        from coinpup_api.ledger.posting import PostingService

        replayed = PostingService(target_engine).post_expense(
            administrator_id,
            financial["ledger_id"],
            financial["request"],
            financial["key"],
        )
        if replayed != financial["receipt"]:
            raise ArchiveError("Restored idempotency replay did not return its original receipt.")
        verify_sealed_journal(target_engine, financial["receipt"].journal_id)
        if before != snapshot(target) or before != snapshot(source):
            raise ArchiveError("Replay or rejected journal append changed restored/source data.")
        print("Backup/restore verified: all application tables and migration rows match exactly.")
        print("Sessions, owned ledgers, multi-asset accounts, categories and archives resolve.")
        print(
            "Exact balances, original idempotency receipt and sealed journal protection verified."
        )
        print("Test databases retained; no DROP ran.")
    finally:
        source_engine.dispose()
        target_engine.dispose()


def main():
    try:
        check_backup_restore()
    except ArchiveError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (psycopg.Error, OSError):
        print(
            "Backup/restore CI database or filesystem check failed; details suppressed.",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            "Backup/restore CI authentication or comparison failed; details suppressed.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
