"""Shared structure tests require a separately migrated disposable PostgreSQL database."""

import os
import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import op
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.admin import create_admin
from coinpup_api.config import Settings
from coinpup_api.database import Database
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.models import Account, AccountAsset, AssetRecord, Category, Entity, Ledger
from coinpup_api.models import Administrator, AuthSession, LoginGuard
from coinpup_api.security import csrf_token_for, token_digest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session


def ocr_schema_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261004_0013_ocr_jobs_and_drafts.py"
        )
    )


def isolated_ocr_schema(engine, action):
    """Only the pre-existing empty 0008 probe removes/reinstates the frozen OCR schema."""
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        assert context.get_current_heads() == ("20261015_0035",)
        with Operations.context(context):
            if action == "downgrade":
                payment_confirmation_migration()[action]()
                confirmation_schema_migration()[action]()
            ocr_schema_migration()[action]()
            if action == "upgrade":
                confirmation_schema_migration()[action]()
                payment_confirmation_migration()[action]()
        assert context.get_current_heads() == ("20261015_0035",)


def confirmation_schema_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261008_0014_ocr_confirmations.py"
        )
    )


def isolated_confirmation_schema(engine, action):
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            if action == "downgrade":
                business_settlement_migration()[action]()
            confirmation_schema_migration()[action]()
            if action == "upgrade":
                business_settlement_migration()[action]()
    if action == "upgrade":
        # An isolated frozen 0014 round trip must restore the current additive type list.
        restore_current_change_types(engine)


def business_reference_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261009_0016_business_references.py"
        )
    )


def draft_line_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261009_0023_draft_lines.py"
        )
    )

    # Keep the frozen draft probe; remove/reinstate its empty new dependent schema first.
    def downgrade():
        business_confirmation_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        business_confirmation_migration()["upgrade"]()

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def business_confirmation_migration(*, include_dependents=True):
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261011_0027_business_confirmations.py"
        )
    )

    if not include_dependents:
        return frozen

    def downgrade():
        business_accrual_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        business_accrual_migration()["upgrade"]()

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def business_accrual_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261011_0028_business_accruals.py"
        )
    )

    def downgrade():
        business_settlement_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        business_settlement_migration()["upgrade"]()

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def business_settlement_migration(*, include_dependents=True):
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261012_0029_business_settlements.py"
        )
    )
    if not include_dependents:
        return frozen

    def downgrade():
        business_payment_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        business_payment_migration()["upgrade"]()

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def business_payment_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261012_0030_business_payments.py"
        )
    )

    def downgrade():
        business_allocation_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        business_allocation_migration()["upgrade"]()

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def payment_confirmation_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261013_0032_ocr_payment_evidence.py"
        )
    )


def allocation_return_storage_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261014_0033_allocation_return_storage.py"
        )
    )


def allocation_return_validation_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261014_0034_allocation_return_validation.py"
        )
    )


def refund_storage_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261015_0035_business_refund_storage.py"
        )
    )


def business_allocation_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261012_0031_business_allocations.py"
        )
    )

    # Preserve the frozen probes; remove/reinstate only their new FK dependent first/last.
    def downgrade():
        refund_storage_migration()["downgrade"]()
        allocation_return_validation_migration()["downgrade"]()
        allocation_return_storage_migration()["downgrade"]()
        payment_confirmation_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        payment_confirmation_migration()["upgrade"]()
        allocation_return_storage_migration()["upgrade"]()
        allocation_return_validation_migration()["upgrade"]()
        refund_storage_migration()["upgrade"]()

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def draft_header_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261009_0022_draft_headers.py"
        )
    )


def project_dimension_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261009_0021_project_dimension.py"
        )
    )


def reminder_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261010_0025_reminder_history.py"
        )
    )

    # Preserve the original probe; remove/reinstate only its empty new FK dependants.
    def downgrade():
        delivery_config_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        delivery_config_migration()["upgrade"]()

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def delivery_config_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261011_0026_reminder_delivery_config.py"
        )
    )

    def upgrade():
        frozen["upgrade"]()
        connection = op.get_bind()
        if connection.exec_driver_sql("SELECT to_regclass('public.business_payments')").scalar():
            # An isolated older round trip must not erase the extant newer type registrations.
            _restore_current_change_types(connection)

    return frozen | {"upgrade": upgrade}


def recurring_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261009_0024_recurring_invoices.py"
        )
    )


def project_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261009_0020_projects.py"
        )
    )


def party_profile_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261009_0019_party_profiles.py"
        )
    )


def period_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261009_0018_ledger_periods.py"
        )
    )


def restore_current_change_types(engine):
    # Isolated frozen OCR migrations replace the check with their historical type list.
    # Reinstate the current additive list only after the original probe completes.
    with engine.begin() as connection:
        _restore_current_change_types(connection)


def _restore_current_change_types(connection):
    connection.exec_driver_sql("ALTER TABLE change_log DROP CONSTRAINT ck_change_log_entity_type")
    connection.exec_driver_sql(
        "ALTER TABLE change_log ADD CONSTRAINT ck_change_log_entity_type CHECK "
        f"(entity_type IN ({refund_storage_migration()['_ENTITY_TYPES']}))"
    )


def change_trigger_registration(engine, *, repair_recreated_file_tables=False):
    """Assert current registration; repair only the known isolated 0008 table round trip."""
    with engine.begin() as connection:
        if connection.exec_driver_sql("SELECT to_regclass('public.change_log')").scalar() is None:
            return
        migration = runpy.run_path(
            str(
                Path(__file__).resolve().parents[2]
                / "services/api/migrations/versions/20261004_0012_ordered_change_log.py"
            )
        )
        definitions = (
            migration["_CHANGE_TRIGGER_SQL"]
            | ocr_schema_migration()["_CHANGE_TRIGGER_SQL"]
            | confirmation_schema_migration()["_CHANGE_TRIGGER_SQL"]
            | business_reference_migration()["_CHANGE_TRIGGER_SQL"]
            | period_migration()["_CHANGE_TRIGGER_SQL"]
            | project_migration()["_CHANGE_TRIGGER_SQL"]
            | recurring_migration()["_CHANGE_TRIGGER_SQL"]
            | reminder_migration()["_CHANGE_TRIGGER_SQL"]
            | delivery_config_migration()["_CHANGE_TRIGGER_SQL"]
            | business_confirmation_migration()["_CHANGE_TRIGGER_SQL"]
            | business_settlement_migration()["_CHANGE_TRIGGER_SQL"]
            | allocation_return_storage_migration()["_CHANGE_TRIGGER_SQL"]
            | refund_storage_migration()["_CHANGE_TRIGGER_SQL"]
        )
        registered = dict(
            connection.exec_driver_sql(
                "SELECT trigger.tgname, relation.relname FROM pg_trigger trigger "
                "JOIN pg_class relation ON relation.oid = trigger.tgrelid "
                "JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace "
                "WHERE namespace.nspname = 'public' AND NOT trigger.tgisinternal"
            ).all()
        )
        for table, definition in definitions.items():
            name = f"trg_{table}_change_log"
            if (
                name not in registered
                and repair_recreated_file_tables
                and table in {"stored_files", "operation_file_links"}
            ):
                connection.exec_driver_sql(definition)
                registered[name] = table
            assert registered.get(name) == table, f"Missing change-log registration for {table}"


@pytest.fixture
def structure_database(request):
    if os.environ.get("COINPUP_RUN_DB_TESTS") != "1":
        pytest.skip("Requires an explicitly configured disposable PostgreSQL database")
    url = os.environ.get("COINPUP_DATABASE_URL")
    if not url:
        pytest.fail("Set COINPUP_DATABASE_URL explicitly for disposable PostgreSQL tests")
    database = Database(Settings(_env_file=None, environment="test", database_url=url))

    def cleanup():
        with database.engine.begin() as connection:
            # Only this explicitly opted-in disposable fixture may truncate immutable journals.
            # RESTRICT rejects unexpected dependants; production utilities never do this.
            connection.exec_driver_sql(
                "TRUNCATE TABLE change_log, ocr_confirmations, ocr_drafts, ocr_jobs, file_uploads, "
                "operation_file_links, stored_files, business_postings, business_confirmations, "
                "business_refund_returns, business_refunds, "
                "business_allocation_return_lines, business_allocation_returns, "
                "business_allocation_lines, business_allocations, business_payments, "
                "reminder_delivery_revisions, reminder_delivery_configs, "
                "reminder_event_revisions, reminder_events, "
                "recurring_invoice_instances, recurring_invoice_rules, "
                "command_receipts, opening_positions, journal_lines, journals, "
                "financial_operations, account_assets, accounts, business_document_lines, "
                "business_documents, business_parties, business_projects, ledger_period_receipts, "
                "ledger_period_audits, ledger_periods RESTRICT"
            )
            connection.execute(delete(AccountAsset))
            connection.execute(delete(Account))
            # Delete leaves first; the immutable-parent rule makes every valid graph acyclic.
            while connection.scalar(select(Category.id).limit(1)) is not None:
                parents = select(Category.parent_id).where(Category.parent_id.is_not(None))
                result = connection.execute(delete(Category).where(Category.id.not_in(parents)))
                if result.rowcount == 0:
                    raise RuntimeError("Invalid category cycle in disposable test database")
            connection.execute(delete(Ledger))
            connection.execute(delete(Entity))
            connection.execute(delete(AssetRecord).where(AssetRecord.asset_id.not_in(list(ASSETS))))
            connection.execute(update(AssetRecord).values(enabled=True, version=1))
            # Resetting builtin assets creates events while the fixture owner still exists.
            # This explicitly disposable cleanup must remove them before deleting that owner.
            connection.exec_driver_sql("TRUNCATE TABLE change_log RESTRICT")
            connection.execute(delete(AuthSession))
            connection.execute(delete(Administrator))
            connection.execute(
                update(LoginGuard).values(
                    failure_count=0, window_started_at=None, locked_until=None
                )
            )

    file_round_trip = (
        request.node.name == "test_empty_documents_migration_round_trip"
        and request.node.path == Path(__file__).parent / "files/test_files_schema.py"
    )
    ocr_detached = False
    confirmation_detached = False
    confirmation_probe = (
        request.node.originalname
        == "test_scoped_migration_guards_data_logs_and_restores_empty_schema"
        and request.node.path == Path(__file__).parent / "ocr/test_ocr_schema.py"
    )
    try:
        change_trigger_registration(database.engine)
        cleanup()
        owner = create_admin(
            database.engine, "structure-test-admin", "fictional-structure-password-2026"
        )
        if file_round_trip:
            # Real FKs prevent independently dropping 0008's files even with empty OCR tables.
            # The frozen downgrade refuses OCR rows/history; no CASCADE or guard bypass occurs.
            isolated_ocr_schema(database.engine, "downgrade")
            ocr_detached = True
        elif confirmation_probe:
            # Preserve the exact frozen 0013 probe, including its RESTRICT and trigger-count checks.
            isolated_confirmation_schema(database.engine, "downgrade")
            confirmation_detached = True
        yield database.engine, owner
    finally:
        if ocr_detached:
            isolated_ocr_schema(database.engine, "upgrade")
        elif confirmation_detached:
            isolated_confirmation_schema(database.engine, "upgrade")
        if ocr_detached:
            restore_current_change_types(database.engine)
        change_trigger_registration(database.engine, repair_recreated_file_tables=file_round_trip)
        cleanup()
        database.close()


@pytest.fixture
def legacy_v1_receipts(structure_database):
    """Seed immutable v1 receipts before the real hash-version schema upgrade."""
    engine, owner = structure_database
    helper = runpy.run_path(str(Path(__file__).parent / "ledger/legacy_v1_receipts.py"))
    return helper["create_legacy_v1_receipts"](engine, owner)


@pytest.fixture
def authenticated_client(structure_database):
    """Real session and HTTP boundary against the explicitly disposable fixture database."""
    from coinpup_api.main import create_app
    from fastapi.testclient import TestClient

    engine, owner = structure_database
    token = "fictional-financial-integration-session"
    with Session(engine) as session, session.begin():
        session.add(
            AuthSession(
                token_hash=token_digest(token),
                administrator_id=owner,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )

    class Probe:
        def __init__(self):
            self.engine = engine

        def check(self):
            pass

        def close(self):
            pass  # The database fixture owns disposal after the HTTP client closes.

    with TestClient(create_app(Settings(_env_file=None, environment="test"), Probe())) as client:
        client.cookies.set("coinpup_session", token)
        client.headers.update(
            {"origin": "http://localhost:8000", "x-csrf-token": csrf_token_for(token)}
        )
        yield client, engine, owner
