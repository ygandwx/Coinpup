"""Opt-in CI exercise. Both URLs must point to disposable test databases; never drops a DB."""

import os
import secrets
import sys
import tempfile
from pathlib import Path

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
    service.create_account(
        owner_id,
        personal.ledger.id,
        AccountCreate(name="Fictional personal Wise", kind="wise", asset_ids=["USD", yen.asset_id]),
    )
    service.create_account(
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
        create_structure_fixture(source_engine, administrator_id)
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
        print("Backup/restore verified: all application tables and migration rows match exactly.")
        print("Sessions, owned ledgers, multi-asset accounts, categories and archives resolve.")
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
