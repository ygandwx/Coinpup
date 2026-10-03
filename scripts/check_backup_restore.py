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
    fx_account = service.create_account(
        owner_id,
        personal.ledger.id,
        AccountCreate(name="Fictional isolated FX account", kind="wise", asset_ids=["USD", "EUR"]),
    )
    btc_account = service.create_account(
        owner_id,
        personal.ledger.id,
        AccountCreate(name="Fictional isolated BTC account", kind="crypto", asset_ids=["BTC"]),
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
    cash_account = service.create_account(
        owner_id,
        company.ledger.id,
        AccountCreate(name="Fictional company USD cash", kind="cash", asset_ids=["USD"]),
    )
    card_account = service.create_account(
        owner_id,
        company.ledger.id,
        AccountCreate(name="Fictional company credit card", kind="credit_card", asset_ids=["USD"]),
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
        "fx_account": fx_account,
        "btc_account": btc_account,
        "company_account": company_account,
        "cash_account": cash_account,
        "card_account": card_account,
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
    if sum(len(ledger["accounts"]) for ledger in ledgers.values()) != 7:
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
        (personal.ledger.id, structure["fx_account"].id, "USD", "1000.00"),
        (personal.ledger.id, structure["btc_account"].id, "BTC", "1.00000000"),
        (company.ledger.id, company_account.id, "USD", "1000.00"),
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
    transfer = create_transfer_fixture(engine, owner_id, structure)
    fees = create_fee_fixture(engine, owner_id, structure)
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
        (str(structure["fx_account"].id), "USD"): "898.00",
        (str(structure["fx_account"].id), "EUR"): "90.00",
        (str(structure["btc_account"].id), "BTC"): "0.89999000",
        (str(company_account.id), "USD"): "700.00",
        (str(structure["cash_account"].id), "USD"): "200.00",
        (str(structure["card_account"].id), "USD"): "0.00",
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
    if "fees" in receipt.model_dump(mode="json"):
        raise ArchiveError("A legacy fee-free expense receipt changed its serialized shape.")
    return {
        "ledger_id": personal.ledger.id,
        "request": request,
        "key": key,
        "receipt": receipt,
        "balances": state,
        "transfer": transfer,
        "fees": fees,
    }


def create_fee_fixture(engine, owner_id, structure):
    """Keep fee examples on separate accounts so earlier balance cases remain unchanged."""
    from coinpup_api.ledger.posting import PostingService
    from coinpup_api.ledger.posting_schemas import ExchangeCreate, ExpenseCreate
    from coinpup_api.ledger.service import LedgerService

    service = PostingService(engine)
    ledger_id = structure["personal"].ledger.id
    fx_account, btc_account = structure["fx_account"], structure["btc_account"]
    expenses = [
        category
        for category in LedgerService(engine).list_categories(owner_id, ledger_id)
        if category.kind == "expense"
    ]
    fx_fee = {
        "account_id": fx_account.id,
        "asset_id": "USD",
        "amount": "2.00",
        "category_id": expenses[0].id,
    }
    btc_fee = {
        "account_id": btc_account.id,
        "asset_id": "BTC",
        "amount": "0.00001000",
        "category_id": expenses[0].id,
    }
    exchange = ExchangeCreate(
        source_account_id=fx_account.id,
        source_asset_id="USD",
        source_amount="100.00",
        destination_account_id=fx_account.id,
        destination_asset_id="EUR",
        destination_amount="90.00",
        transaction_date=date(2026, 2, 5),
        description="Fictional same-account exchange with explicit fee",
        fees=[fx_fee],
    )
    payment = ExpenseCreate(
        account_id=btc_account.id,
        asset_id="BTC",
        amount="0.10000000",
        transaction_date=date(2026, 2, 6),
        recognition_date=date(2026, 2, 6),
        description="Fictional BTC purchase with network fee",
        splits=[{"category_id": expenses[1].id, "amount": "0.10000000"}],
        fees=[btc_fee],
    )
    fixtures = []
    for method, request, fee in (
        ("post_exchange", exchange, fx_fee),
        ("post_expense", payment, btc_fee),
    ):
        key = str(uuid4())
        receipt = getattr(service, method)(owner_id, ledger_id, request, key)
        expected_fee = {
            field: str(value) if field in {"account_id", "category_id"} else value
            for field, value in fee.items()
        }
        if receipt.model_dump(mode="json").get("fees") != [expected_fee]:
            raise ArchiveError("CI fee receipt does not preserve the separate exact fee.")
        if service.get_operation(owner_id, ledger_id, receipt.id) != receipt:
            raise ArchiveError("CI principal/fee operation could not be read without changing it.")
        fixtures.append(
            {
                "method": method,
                "ledger_id": ledger_id,
                "request": request,
                "key": key,
                "receipt": receipt,
            }
        )
    if (
        fixtures[0]["receipt"].source_amount != "100.00"
        or fixtures[0]["receipt"].destination_amount != "90.00"
        or fixtures[1]["receipt"].amount != "0.10000000"
    ):
        raise ArchiveError("A fee changed its operation's original principal quantities.")
    return fixtures


def company_classification(engine, ledger_id):
    """Income/expense roles exclude opening equity and transferred principal."""
    from coinpup_api.ledger import Amount, get_asset
    from coinpup_api.ledger.models import JournalLine
    from sqlalchemy import func, select

    with engine.connect() as connection:
        totals = dict(
            connection.execute(
                select(JournalLine.role, func.sum(JournalLine.amount))
                .where(
                    JournalLine.ledger_id == ledger_id,
                    JournalLine.asset_id == "USD",
                    JournalLine.role.in_(["income", "expense"]),
                )
                .group_by(JournalLine.role)
            ).all()
        )
    return {
        role: Amount.from_decimal(totals[role], get_asset("USD")).to_string()
        if role in totals
        else "0.00"
        for role in ("income", "expense")
    }


def create_transfer_fixture(engine, owner_id, structure):
    from coinpup_api.ledger.posting import PostingService
    from coinpup_api.ledger.posting_schemas import ExpenseCreate, TransferCreate
    from coinpup_api.ledger.schemas import AccountUpdate
    from coinpup_api.ledger.service import LedgerService

    service = PostingService(engine)
    ledger_id = structure["company"].ledger.id
    bank, cash, card = (
        structure["company_account"],
        structure["cash_account"],
        structure["card_account"],
    )
    request = TransferCreate(
        source_account_id=bank.id,
        destination_account_id=cash.id,
        asset_id="USD",
        amount="200.00",
        transaction_date=date(2026, 2, 1),
        description="Fictional bank to cash transfer",
    )
    key = str(uuid4())
    receipt = service.post_transfer(owner_id, ledger_id, request, key)
    expense_category = next(
        category
        for category in LedgerService(engine).list_categories(owner_id, ledger_id)
        if category.kind == "expense"
    )
    service.post_expense(
        owner_id,
        ledger_id,
        ExpenseCreate(
            account_id=card.id,
            asset_id="USD",
            amount="100.00",
            transaction_date=date(2026, 2, 3),
            recognition_date=date(2026, 2, 3),
            description="Fictional credit card purchase",
            splits=[{"category_id": expense_category.id, "amount": "100.00"}],
        ),
        str(uuid4()),
    )
    card_balances = service.balances(owner_id, ledger_id, account_id=card.id)
    if len(card_balances) != 1 or card_balances[0].amount != "-100.00":
        raise ArchiveError("Fictional card purchase did not create its signed liability position.")
    service.post_transfer(
        owner_id,
        ledger_id,
        TransferCreate(
            source_account_id=bank.id,
            destination_account_id=card.id,
            asset_id="USD",
            amount="100.00",
            transaction_date=date(2026, 2, 4),
            description="Fictional credit card repayment",
        ),
        str(uuid4()),
    )
    if company_classification(engine, ledger_id) != {"income": "0.00", "expense": "100.00"}:
        raise ArchiveError("Transfer or card repayment changed the expected income/expense total.")
    if service.get_operation(owner_id, ledger_id, receipt.id) != receipt:
        raise ArchiveError(
            "Transfer could not be read with its original source/destination receipt."
        )
    if "fees" in receipt.model_dump(mode="json"):
        raise ArchiveError("A legacy fee-free transfer receipt changed its serialized shape.")
    # The saved receipt must still replay after its destination becomes archived.
    LedgerService(engine).update_account(
        owner_id,
        ledger_id,
        cash.id,
        AccountUpdate(expected_version=cash.version, archived=True),
    )
    return {"ledger_id": ledger_id, "request": request, "key": key, "receipt": receipt}


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


def verify_sealed_journal(engine, journal_id, component_no=None):
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
            if not originals:
                raise ArchiveError("CI sealed journal fixture has no lines.")
            highest_line = max(row["line_no"] for row in originals)
            if component_no is not None:
                originals = [row for row in originals if row["component_no"] == component_no]
            if len(originals) < 2:
                raise ArchiveError("CI sealed journal fixture has too few lines.")
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
            if company_classification(engine, structure["company"].ledger.id) != {
                "income": "0.00",
                "expense": "100.00",
            }:
                raise ArchiveError(
                    "Restored or original company expense includes transfer principal."
                )
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
        transfer = financial["transfer"]
        service = PostingService(target_engine)
        transfer_replay = service.post_transfer(
            administrator_id, transfer["ledger_id"], transfer["request"], transfer["key"]
        )
        if (
            transfer_replay != transfer["receipt"]
            or service.get_operation(
                administrator_id, transfer["ledger_id"], transfer["receipt"].id
            )
            != transfer["receipt"]
        ):
            raise ArchiveError(
                "Restored transfer replay/read changed its original two-account receipt."
            )
        verify_sealed_journal(target_engine, transfer["receipt"].journal_id)
        for fixture in financial["fees"]:
            fee_replay = getattr(service, fixture["method"])(
                administrator_id, fixture["ledger_id"], fixture["request"], fixture["key"]
            )
            if (
                fee_replay != fixture["receipt"]
                or service.get_operation(
                    administrator_id, fixture["ledger_id"], fixture["receipt"].id
                )
                != fixture["receipt"]
            ):
                raise ArchiveError("Restored principal/fee read or replay changed its receipt.")
            listed = {
                operation.id: operation
                for operation in service.list_operations(administrator_id, fixture["ledger_id"])
            }
            if listed.get(fixture["receipt"].id) != fixture["receipt"]:
                raise ArchiveError("Restored operation list omitted or changed a fee receipt.")
            verify_sealed_journal(target_engine, fixture["receipt"].journal_id, component_no=1)
        if before != snapshot(target) or before != snapshot(source):
            raise ArchiveError("Replay or rejected journal append changed restored/source data.")
        print("Backup/restore verified: all application tables and migration rows match exactly.")
        print("Sessions, owned ledgers, multi-asset accounts, categories and archives resolve.")
        print(
            "Exact balances, original idempotency receipt and sealed journal protection verified."
        )
        print("Bank/cash transfer and card repayment preserve balances without duplicate expense.")
        print("FX and BTC principal/fees remain exact; fee-component journals remain sealed.")
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
