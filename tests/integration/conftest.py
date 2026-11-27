"""Shared structure tests require a separately migrated disposable PostgreSQL database."""

import os
import runpy
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import op
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from coinpup_api.admin import create_admin
from coinpup_api.config import Settings
from coinpup_api.database import Database
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.models import Account, AccountAsset, AssetRecord, Category, Entity, Ledger
from coinpup_api.models import Administrator, AuthSession, LoginGuard
from coinpup_api.security import csrf_token_for, token_digest
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

# Frozen migration probes preserve whichever additive layers belong to this checkout.
_CURRENT_HEADS = tuple(
    ScriptDirectory.from_config(
        Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    ).get_heads()
)


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
        assert context.get_current_heads() == _CURRENT_HEADS
        with Operations.context(context):
            if action == "downgrade":
                payment_confirmation_migration()[action]()
                confirmation_schema_migration()[action]()
            ocr_schema_migration()[action]()
            if action == "upgrade":
                confirmation_schema_migration()[action]()
                payment_confirmation_migration()[action]()
        assert context.get_current_heads() == _CURRENT_HEADS


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

    return _with_document_headers(frozen | {"downgrade": downgrade, "upgrade": upgrade})


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

    return _with_document_source_bindings(frozen | {"downgrade": downgrade, "upgrade": upgrade})


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

    return _with_allocation_document_facts(frozen | {"downgrade": downgrade, "upgrade": upgrade})


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


def document_accrual_fact_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261110_0080_document_accrual_posting_facts.py"
        )
    )


def document_accrual_head_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261110_0081_document_accrual_operation_heads.py"
        )
    )


def document_receipt_seal_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261110_0082_document_revision_receipt_seals.py"
        )
    )


def restore_document_receipt_trigger(table):
    # Historical schema probes recreate these tables from their original frozen definitions.
    if op.get_bind().scalar(
        text("SELECT to_regprocedure('public.coinpup_reject_sealed_document_member()') IS NOT NULL")
    ):
        op.execute(document_receipt_seal_migration()["_TRIGGERS"][table])


def document_accrual_member_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261110_0078_document_accrual_members.py"
        )
    )

    def installed(signature):
        return op.get_bind().scalar(
            text("SELECT to_regprocedure(:fn) IS NOT NULL"), {"fn": signature}
        )

    def downgrade():
        if installed("public.coinpup_check_document_operation_head()"):
            op.execute(document_accrual_head_migration()["_ORIGINAL"])
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        # A historical probe recreates 0078's table and the 0027 posting dispatcher.
        if installed("public.coinpup_check_document_accrual_facts()"):
            op.execute(document_accrual_fact_migration()["_TRIGGER"])
        if installed("public.coinpup_check_document_operation_head()"):
            current = document_accrual_head_migration()
            op.execute(current["_DISPATCH"])
            op.execute(current["_TRIGGER"])
        restore_document_receipt_trigger("business_document_accruals")
        # 0078 predates the independent Invoice configuration table. Its direct
        # frozen round trip must restore the later notification type as well.
        bind = op.get_bind()
        if MigrationContext.configure(bind).get_current_heads() == _CURRENT_HEADS and bind.scalar(
            text("SELECT to_regclass('public.business_invoice_profiles') IS NOT NULL")
        ):
            _restore_current_change_types(bind)

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def document_source_binding_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261110_0077_document_revision_line_identities.py"
        )
    )

    def downgrade():
        document_accrual_member_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        document_accrual_member_migration()["upgrade"]()

    return frozen | {"upgrade": upgrade, "downgrade": downgrade}


def _with_document_source_bindings(migration):
    def installed():
        return (
            op.get_bind()
            .exec_driver_sql(
                "SELECT to_regprocedure('public.coinpup_bind_document_revision_source()') "
                "IS NOT NULL"
            )
            .scalar_one()
        )

    def downgrade():
        if installed():
            document_source_binding_migration()["downgrade"]()
        migration["downgrade"]()

    def upgrade():
        migration["upgrade"]()
        if (
            op.get_bind()
            .exec_driver_sql(
                "SELECT to_regclass('public.business_document_revision_lines') IS NOT NULL"
            )
            .scalar_one()
            and not installed()
        ):
            document_source_binding_migration()["upgrade"]()

    return migration | {"downgrade": downgrade, "upgrade": upgrade}


def allocation_document_validation_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions"
            / "20261110_0085_allocation_document_validation.py"
        )
    )


def allocation_document_fact_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261110_0084_allocation_document_facts.py"
        )
    )


def _with_allocation_document_facts(migration):
    # Historical probes can rebuild either side of the allocation-to-priced-line FK.
    def installed():
        return op.get_bind().scalar(
            text(
                "SELECT to_regprocedure('public.coinpup_bind_allocation_document_fact()') "
                "IS NOT NULL"
            )
        )

    def downgrade():
        if installed():
            allocation_document_fact_migration()["downgrade"]()
        migration["downgrade"]()

    def upgrade():
        migration["upgrade"]()
        bind = op.get_bind()
        if (
            MigrationContext.configure(bind).get_current_heads() == _CURRENT_HEADS
            and not installed()
            and bind.scalar(
                text(
                    "SELECT to_regclass('public.business_allocation_lines') "
                    "IS NOT NULL AND to_regclass('public.business_document_revision_lines') "
                    "IS NOT NULL"
                )
            )
        ):
            allocation_document_fact_migration()["upgrade"]()

    return migration | {"downgrade": downgrade, "upgrade": upgrade}


def document_revision_line_migration(*, include_dependents=True):
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions"
            / "20261110_0076_confirmed_document_revision_lines.py"
        )
    )

    if not include_dependents:
        return frozen

    def upgrade():
        frozen["upgrade"]()
        restore_document_receipt_trigger("business_document_revision_lines")

    return _with_allocation_document_facts(
        _with_document_source_bindings(frozen | {"upgrade": upgrade})
    )


def credit_document_source_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261113_0088_credit_document_sources.py"
        )
    )

    def upgrade():
        frozen["upgrade"]()
        if MigrationContext.configure(op.get_bind()).get_current_heads() == _CURRENT_HEADS:
            credit_source_plan_migration()["upgrade"]()
        _restore_credit_document_origin()

    return frozen | {"upgrade": upgrade}


def credit_document_origin_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261113_0090_credit_document_origin.py"
        )
    )


def credit_source_plan_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261113_0092_credit_source_query_plans.py"
        )
    )


def document_lifecycle_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261113_0096_document_group_lifecycle.py"
        )
    )


def document_group_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261113_0095_document_group_proof.py"
        )
    )


def document_dependency_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261113_0094_document_dependency_proof.py"
        )
    )


def credit_bound_receipt_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261113_0093_credit_bound_revision_receipts.py"
        )
    )


def _restore_credit_bound_receipt():
    bind = op.get_bind()
    if MigrationContext.configure(bind).get_current_heads() == _CURRENT_HEADS and bind.scalar(
        text(
            "SELECT to_regprocedure('public.coinpup_bind_credit_document_version()') IS NOT NULL "
            "AND to_regprocedure('public.coinpup_business_credit_revision_response(uuid)') "
            "IS NOT NULL"
        )
    ):
        credit_bound_receipt_migration()["upgrade"]()


def _restore_credit_document_origin():
    # The SQL receipt projection needs both the old group tables and the new bound header.
    bind = op.get_bind()
    if MigrationContext.configure(bind).get_current_heads() == _CURRENT_HEADS and bind.scalar(
        text(
            "SELECT to_regprocedure('public.coinpup_bind_credit_document_version()') IS NOT NULL "
            "AND to_regprocedure('public.coinpup_validate_credit_origin_facts(uuid)') IS NOT NULL"
        )
    ):
        credit_document_origin_migration()["upgrade"]()
        _restore_credit_document_capacity()
        _restore_credit_bound_receipt()


def credit_document_capacity_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261113_0091_credit_document_capacity.py"
        )
    )


def _restore_credit_document_capacity():
    bind = op.get_bind()
    if _CREDIT_HISTORY_LAYER.get() or _REFUND_HISTORY_LAYER.get():
        return
    if MigrationContext.configure(bind).get_current_heads() == _CURRENT_HEADS and bind.scalar(
        text(
            "SELECT to_regprocedure('public.coinpup_bind_credit_document_version()') IS NOT NULL "
            "AND to_regprocedure('public.coinpup_validate_business_credit(uuid)') IS NOT NULL"
        )
    ):
        credit_document_capacity_migration()["upgrade"]()


def _with_credit_document_sources(migration):
    # Historical probes can remove either header table of the new immutable binding.
    def installed():
        return op.get_bind().scalar(
            text(
                "SELECT to_regprocedure('public.coinpup_bind_credit_document_version()') "
                "IS NOT NULL"
            )
        )

    def downgrade():
        if installed():
            credit_document_source_migration()["downgrade"]()
        migration["downgrade"]()

    def upgrade():
        migration["upgrade"]()
        bind = op.get_bind()
        if (
            MigrationContext.configure(bind).get_current_heads() == _CURRENT_HEADS
            and not installed()
            and bind.scalar(
                text(
                    "SELECT to_regclass('public.business_credits') IS NOT NULL "
                    "AND to_regclass('public.business_credit_revisions') IS NOT NULL "
                    "AND to_regclass('public.business_document_revisions') IS NOT NULL"
                )
            )
        ):
            credit_document_source_migration()["upgrade"]()

    return migration | {"downgrade": downgrade, "upgrade": upgrade}


def document_revision_header_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions"
            / "20261110_0075_confirmed_document_revision_headers.py"
        )
    )

    # A direct historical probe must detach the newer FK and restore its function/catalog.
    def downgrade():
        document_revision_line_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        restore_document_receipt_trigger("business_document_revisions")
        document_revision_line_migration()["upgrade"]()
        if not _DOCUMENT_HISTORY_LAYER.get():
            document_lifecycle_migration()["upgrade"]()

    return _with_credit_document_sources(frozen | {"upgrade": upgrade, "downgrade": downgrade})


def _with_document_headers(migration):
    # The frozen 0027 and 0061 probes temporarily remove a referenced table or restore
    # a historical change-feed constraint. Reinstall 0075 only after both are present.
    def present(table):
        return (
            op.get_bind()
            .execute(text("SELECT to_regclass(:table) IS NOT NULL"), {"table": "public." + table})
            .scalar_one()
        )

    def downgrade():
        if present("business_document_revisions"):
            document_revision_header_migration()["downgrade"]()
        migration["downgrade"]()

    def upgrade():
        migration["upgrade"]()
        if (
            not present("business_document_revisions")
            and present("business_confirmations")
            and present("business_return_revision_members")
        ):
            document_revision_header_migration()["upgrade"]()

    return migration | {"downgrade": downgrade, "upgrade": upgrade}


def return_lifecycle_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261109_0074_return_group_lifecycle.py"
        )
    )


def _with_return_lifecycle(migration):
    """Restore 0074 only after all frozen definitions are back after nested DDL probes."""

    def downgrade():
        connection = op.get_bind()
        active = connection.exec_driver_sql(
            "SELECT to_regprocedure('coinpup_require_return_refund_dates("
            "uuid,uuid,uuid,varchar,date)')"
        ).scalar_one()
        if active is not None:
            return_lifecycle_migration()["downgrade"]()
        migration["downgrade"]()

    def upgrade():
        migration["upgrade"]()
        lifecycle = return_lifecycle_migration()
        expected = {
            name: (lifecycle["_FROZEN"] / f"{name}.sql").read_text(encoding="utf-8")
            for name in lifecycle["_PREDECESSORS"]
        }
        # Some tests detach 0033/0034 and 0059/0060/0061 in independent calls. Checking just
        # for a table would reinstall the active proof over an older, still-detached validator.
        actual = dict(
            op.get_bind()
            .execute(
                text(
                    "SELECT proname,pg_get_functiondef(oid) FROM pg_proc "
                    "WHERE pronamespace='public'::regnamespace AND proname=ANY(:names)"
                ),
                {"names": list(expected)},
            )
            .all()
        )
        capacity_ready = (
            op.get_bind()
            .exec_driver_sql(
                "SELECT to_regprocedure('coinpup_returned_payment_amount(uuid,uuid)') IS NOT NULL"
            )
            .scalar_one()
        )
        if capacity_ready and actual == expected:
            lifecycle["upgrade"]()

    return migration | {"downgrade": downgrade, "upgrade": upgrade}


def allocation_return_storage_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261014_0033_allocation_return_storage.py"
        )
    )
    return _with_return_lifecycle(frozen)


def allocation_return_validation_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261014_0034_allocation_return_validation.py"
        )
    )
    return _with_return_lifecycle(frozen)


def refund_storage_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261015_0035_business_refund_storage.py"
        )
    )


_DOCUMENT_HISTORY_LAYER = ContextVar("document_history_layer", default=False)


_CREDIT_HISTORY_LAYER = ContextVar("credit_history_layer", default=False)


_REFUND_HISTORY_LAYER = ContextVar("refund_history_layer", default=False)


_INSIDE_BOUND_DATE_ROUNDTRIP = ContextVar("inside_bound_date_roundtrip", default=False)


def refund_bound_date_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261109_0071_refund_bound_evidence_dates.py"
        )
    )


def bound_refund_evidence_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261109_0070_refund_bound_return_versions.py"
        )
    )
    return _with_dependent_migration(
        frozen, refund_bound_date_migration, _INSIDE_BOUND_DATE_ROUNDTRIP
    )


_INSIDE_EVIDENCE_ROUNDTRIP = ContextVar("inside_evidence_roundtrip", default=False)


def _with_dependent_migration(migration, load_dependent, nesting, *, detached=None):
    """Run each frozen dependency once around nested historical DDL, in schema order."""

    def run(action):
        if nesting.get() or (detached is not None and detached.get()):
            migration[action]()
            return
        token = nesting.set(True)
        try:
            dependent = load_dependent()
            if action == "downgrade":
                dependent[action]()
            migration[action]()
            if action == "upgrade":
                dependent[action]()
        finally:
            nesting.reset(token)

    return migration | {"downgrade": lambda: run("downgrade"), "upgrade": lambda: run("upgrade")}


def _with_refund_evidence_binding(migration):
    # The 0035 and 0050 tables own the 0070 column and binding triggers.
    return _with_dependent_migration(
        migration, bound_refund_evidence_migration, _INSIDE_EVIDENCE_ROUNDTRIP
    )


def return_ownership_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261109_0069_return_operation_ownership.py"
        )
    )


def return_document_capacity_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions"
            / "20261110_0087_effective_return_document_capacity.py"
        )
    )


def return_document_source_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions"
            / "20261110_0086_return_document_sources.py"
        )
    )


def _with_pinned_return_definition(migration, definition):
    def upgrade():
        migration["upgrade"]()
        if MigrationContext.configure(op.get_bind()).get_current_heads() == _CURRENT_HEADS:
            op.execute(return_document_source_migration()[definition])

    return migration | {"upgrade": upgrade}


def return_group_receipt_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261109_0068_return_revision_receipts.py"
        )
    )
    return _with_pinned_return_definition(frozen, "_RECEIPT")


def return_source_sequence_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261109_0066_return_source_sequences.py"
        )
    )
    return _with_pinned_return_definition(frozen, "_SEQUENCE")


def return_historical_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261109_0065_return_historical_postings.py"
        )
    )


def effective_return_capacity_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261109_0064_effective_return_capacity.py"
        )
    )
    effective = _with_return_lifecycle(frozen)

    def upgrade():
        effective["upgrade"]()
        # 0064 owns the predecessor of the allocation validator replaced at 0085.
        if MigrationContext.configure(op.get_bind()).get_current_heads() == _CURRENT_HEADS:
            allocation_document_validation_migration()["upgrade"]()
        _restore_credit_document_capacity()

    return effective | {"upgrade": upgrade}


_INSIDE_CAPACITY_ROUNDTRIP = ContextVar("inside_capacity_roundtrip", default=False)


def _with_effective_return_capacity(migration):
    return _with_dependent_migration(
        migration,
        effective_return_capacity_migration,
        _INSIDE_CAPACITY_ROUNDTRIP,
        detached=_REFUND_HISTORY_LAYER,
    )


def return_revision_member_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261109_0061_allocation_return_revision_members.py"
        )
    )
    return _with_document_headers(_with_return_lifecycle(frozen))


def return_revision_header_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261109_0059_allocation_return_revision_headers.py"
        )
    )
    guard = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261109_0060_return_current_refund_evidence.py"
        )
    )

    def downgrade():
        return_ownership_migration()["downgrade"]()
        return_revision_member_migration()["downgrade"]()
        guard["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        guard["upgrade"]()
        return_revision_member_migration()["upgrade"]()
        return_ownership_migration()["upgrade"]()

    return _with_return_lifecycle(frozen | {"downgrade": downgrade, "upgrade": upgrade})


def refund_lifecycle_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261109_0057_refund_revision_lifecycle.py"
        )
    )
    return _with_effective_return_capacity(frozen)


def refund_revision_guards_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261109_0056_refund_revision_guards.py"
        )
    )


def refund_fixed_receipt_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261109_0055_refund_revision_receipt.py"
        )
    )


def refund_historical_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261109_0054_refund_historical_validation.py"
        )
    )
    return _with_dependent_migration(
        frozen,
        refund_bound_date_migration,
        _INSIDE_BOUND_DATE_ROUNDTRIP,
        detached=_INSIDE_EVIDENCE_ROUNDTRIP,
    )


def refund_posting_fact_migration(*, include_dependents=True):
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261109_0053_refund_posting_fact.py"
        )
    )

    if not include_dependents:
        return frozen

    def downgrade():
        refund_fixed_receipt_migration()["downgrade"]()
        refund_historical_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        refund_historical_migration()["upgrade"]()
        refund_fixed_receipt_migration()["upgrade"]()

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def refund_current_capacity_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261109_0052_refund_current_capacity.py"
        )
    )

    def downgrade():
        if not _REFUND_HISTORY_LAYER.get():
            refund_lifecycle_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        if not _REFUND_HISTORY_LAYER.get():
            refund_lifecycle_migration()["upgrade"]()

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def refund_principal_migration(*, include_dependents=True):
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261109_0051_refund_principal_projection.py"
        )
    )

    if not include_dependents:
        return frozen

    def downgrade():
        refund_current_capacity_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        refund_current_capacity_migration()["upgrade"]()

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def refund_revision_evidence_migration(*, include_dependents=True):
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261109_0050_refund_revision_evidence.py"
        )
    )

    if not include_dependents:
        return frozen

    def downgrade():
        refund_posting_fact_migration()["downgrade"]()
        refund_principal_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        refund_principal_migration()["upgrade"]()
        refund_posting_fact_migration()["upgrade"]()

    return _with_refund_evidence_binding(frozen | {"downgrade": downgrade, "upgrade": upgrade})


def refund_revision_header_migration(*, include_dependents=True):
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261109_0049_refund_revision_headers.py"
        )
    )

    if not include_dependents:
        return frozen

    def downgrade():
        refund_revision_guards_migration()["downgrade"]()
        refund_revision_evidence_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        refund_revision_evidence_migration()["upgrade"]()
        refund_revision_guards_migration()["upgrade"]()

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def credit_lifecycle_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261106_0048_credit_revision_lifecycle.py"
        )
    )
    return _with_effective_return_capacity(frozen)


def credit_revision_guards_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261106_0047_credit_revision_guards.py"
        )
    )

    def downgrade():
        if not _CREDIT_HISTORY_LAYER.get():
            credit_lifecycle_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        if not _CREDIT_HISTORY_LAYER.get():
            credit_lifecycle_migration()["upgrade"]()

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def credit_current_capacity_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261106_0046_credit_current_capacity.py"
        )
    )

    def has_refund_projection():
        return (
            not _CREDIT_HISTORY_LAYER.get()
            and op.get_bind()
            .exec_driver_sql(
                "SELECT to_regprocedure('public.coinpup_current_refund_principals(uuid,uuid[])')"
            )
            .scalar()
        )

    def downgrade():
        if has_refund_projection():
            refund_current_capacity_migration()["downgrade"]()
        credit_revision_guards_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        credit_revision_guards_migration()["upgrade"]()
        if has_refund_projection():
            refund_current_capacity_migration()["upgrade"]()

    return _with_effective_return_capacity(frozen | {"downgrade": downgrade, "upgrade": upgrade})


def credit_receipt_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261106_0045_credit_revision_receipts.py"
        )
    )

    def downgrade():
        credit_current_capacity_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        credit_current_capacity_migration()["upgrade"]()
        _restore_credit_bound_receipt()

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def credit_group_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261106_0044_credit_group_transitions.py"
        )
    )

    def downgrade():
        credit_receipt_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        credit_receipt_migration()["upgrade"]()

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def credit_document_validation_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261113_0089_credit_document_validation.py"
        )
    )


def credit_fact_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261106_0043_credit_fact_validation.py"
        )
    )

    def downgrade():
        # A nested historical probe may already have detached the newer document tables.
        if op.get_bind().scalar(text("SELECT to_regclass('public.business_document_revisions')")):
            credit_document_validation_migration()["downgrade"]()
        credit_group_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        credit_group_migration()["upgrade"]()
        if MigrationContext.configure(op.get_bind()).get_current_heads() == _CURRENT_HEADS:
            credit_document_validation_migration()["upgrade"]()
        _restore_credit_document_origin()

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def credit_projection_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261106_0042_credit_read_projections.py"
        )
    )

    def downgrade():
        credit_fact_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        credit_fact_migration()["upgrade"]()
        # The historical 0042 probe recreates the original SQL projection.
        # Restore the matching 0092 plan only when its companion is actually installed.
        prepared = (
            op.get_bind()
            .exec_driver_sql(
                "SELECT l.lanname='plpgsql' FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang "
                "WHERE p.oid=to_regprocedure("
                "'public.coinpup_credit_document_sources(uuid,uuid[],uuid[])')"
            )
            .scalar()
        )
        if prepared:
            credit_source_plan_migration()["upgrade"]()

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def credit_revision_member_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261106_0041_credit_revision_members.py"
        )
    )

    def downgrade():
        credit_projection_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        credit_projection_migration()["upgrade"]()
        # Frozen 0041 reinstates its own historical sync types during isolated round trips.
        connection = op.get_bind()
        if connection.exec_driver_sql(
            "SELECT to_regclass('public.business_refund_revisions')"
        ).scalar():
            _restore_current_change_types(connection)

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def credit_revision_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261106_0040_credit_revision_headers.py"
        )
    )

    def downgrade():
        credit_revision_member_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        credit_revision_member_migration()["upgrade"]()

    return _with_credit_document_sources(frozen | {"downgrade": downgrade, "upgrade": upgrade})


def credit_validation_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261021_0039_business_credit_validation.py"
        )
    )

    def downgrade():
        credit_revision_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        credit_revision_migration()["upgrade"]()
        bind = op.get_bind()
        if bind.scalar(text("SELECT to_regclass('public.interledger_groups') IS NOT NULL")):
            # The frozen 0039 layer recreates the shared kind check and dispatcher.
            current = runpy.run_path(
                str(
                    Path(__file__).resolve().parents[2]
                    / "services/api/migrations/versions/20261125_0106_interledger.py"
                )
            )
            current["_install_operation_kinds"](current["_INTERLEDGER_KINDS"])
            current["_install_interledger_dispatch"]()

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def credit_capacity_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261021_0038_business_credit_capacity.py"
        )
    )

    def downgrade():
        credit_validation_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        credit_validation_migration()["upgrade"]()

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def credit_storage_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261021_0037_business_credit_storage.py"
        )
    )

    def downgrade():
        credit_capacity_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        credit_capacity_migration()["upgrade"]()

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def refund_validation_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261015_0036_business_refund_validation.py"
        )
    )

    # Preserve the frozen probes while removing/reinstating the new empty dependent schema.
    def downgrade():
        return_revision_header_migration()["downgrade"]()
        refund_revision_header_migration()["downgrade"]()
        credit_storage_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        credit_storage_migration()["upgrade"]()
        refund_revision_header_migration()["upgrade"]()
        return_revision_header_migration()["upgrade"]()

    return _with_refund_evidence_binding(
        _with_effective_return_capacity(frozen | {"downgrade": downgrade, "upgrade": upgrade})
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
        refund_validation_migration()["downgrade"]()
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
        refund_validation_migration()["upgrade"]()

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


def resumable_upload_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261125_0111_resumable_uploads.py"
        )
    )


def financial_device_push_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261125_0110_financial_device_push.py"
        )
    )


def device_checkpoints_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261125_0109_device_checkpoints.py"
        )
    )

    def downgrade():
        bind = op.get_bind()
        if bind.scalar(text("SELECT to_regclass('public.file_upload_resumes') IS NOT NULL")):
            resumable_upload_migration()["downgrade"]()
        if bind.scalar(text("SELECT to_regclass('public.sync_financial_pushes') IS NOT NULL")):
            financial_device_push_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        if MigrationContext.configure(op.get_bind()).get_current_heads() == _CURRENT_HEADS:
            financial_device_push_migration()["upgrade"]()
            resumable_upload_migration()["upgrade"]()

    return frozen | {"downgrade": downgrade, "upgrade": upgrade}


def reminder_email_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261125_0108_reminder_email.py"
        )
    )


def reminder_inbox_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261125_0113_reminder_inbox.py"
        )
    )


def delivery_config_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261011_0026_reminder_delivery_config.py"
        )
    )

    def downgrade():
        connection = op.get_bind()
        if connection.exec_driver_sql(
            "SELECT to_regclass('public.reminder_inbox_deliveries')"
        ).scalar():
            reminder_inbox_migration()["downgrade"]()
        if connection.exec_driver_sql(
            "SELECT to_regclass('public.reminder_email_deliveries')"
        ).scalar():
            # The exact older contract does not know about its empty new dependants.
            reminder_email_migration()["downgrade"]()
        frozen["downgrade"]()

    def upgrade():
        frozen["upgrade"]()
        connection = op.get_bind()
        on_email_head = MigrationContext.configure(connection).get_current_heads() == _CURRENT_HEADS
        if on_email_head:
            reminder_email_migration()["upgrade"]()
            reminder_inbox_migration()["upgrade"]()
        if connection.exec_driver_sql("SELECT to_regclass('public.business_payments')").scalar():
            # An isolated older round trip must not erase the extant newer type registrations.
            _restore_current_change_types(connection)

    return frozen | {"upgrade": upgrade, "downgrade": downgrade}


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


_INVOICE_PROFILE_ROUNDTRIP = ContextVar("invoice_profile_roundtrip", default=False)


_INVOICE_RESERVATION_ROUNDTRIP = ContextVar("invoice_reservation_roundtrip", default=False)
_INVOICE_NUMBERING_HISTORY_LAYER = ContextVar("invoice_numbering_history", default=False)
_INVOICE_NUMBERING_ROUNDTRIP = ContextVar("invoice_numbering_roundtrip", default=False)
_INVOICE_SNAPSHOT_HISTORY_LAYER = ContextVar("invoice_snapshot_history", default=False)
_INVOICE_SNAPSHOT_ROUNDTRIP = ContextVar("invoice_snapshot_roundtrip", default=False)
_INVOICE_DOCUMENT_HISTORY_LAYER = ContextVar("invoice_document_history", default=False)
_INVOICE_DOCUMENT_ROUNDTRIP = ContextVar("invoice_document_roundtrip", default=False)
_INVOICE_KEY_HISTORY_LAYER = ContextVar("invoice_key_history", default=False)
_INVOICE_KEY_ROUNDTRIP = ContextVar("invoice_key_roundtrip", default=False)
_INVOICE_COMPLETION_ROUNDTRIP = ContextVar("invoice_completion_roundtrip", default=False)
_INVOICE_PREVIOUS_HISTORY_LAYER = ContextVar("invoice_previous_history", default=False)


_INVOICE_ACTIVATION_ROUNDTRIP = ContextVar("invoice_activation_roundtrip", default=False)
_INVOICE_CLOSED_LAYER = ContextVar("invoice_closed_layer", default=False)


def invoice_activation_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261114_0105_invoice_reservation_activation.py"
        )
    )


def invoice_completion_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261114_0104_invoice_completions.py"
        )
    )

    def upgrade():
        frozen["upgrade"]()
        bind = op.get_bind()
        if bind.scalar(text("SELECT to_regclass('public.interledger_groups') IS NOT NULL")):
            # A direct 0104 round trip keeps subsequent, independently installed tables.
            _restore_current_change_types(bind)

    return frozen | {"upgrade": upgrade}


def invoice_predecessor_migration():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261114_0103_invoice_predecessor.py"
        )
    )


def invoice_key_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261113_0102_invoice_command_key.py"
        )
    )
    return _with_dependent_migration(
        frozen,
        invoice_predecessor_migration,
        _INVOICE_KEY_ROUNDTRIP,
        detached=_INVOICE_PREVIOUS_HISTORY_LAYER,
    )


def invoice_document_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261113_0101_invoice_document_binding.py"
        )
    )
    return _with_dependent_migration(
        frozen,
        invoice_key_migration,
        _INVOICE_DOCUMENT_ROUNDTRIP,
        detached=_INVOICE_KEY_HISTORY_LAYER,
    )


def invoice_snapshot_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261113_0100_invoice_snapshot_binding.py"
        )
    )
    return _with_dependent_migration(
        frozen,
        invoice_document_migration,
        _INVOICE_SNAPSHOT_ROUNDTRIP,
        detached=_INVOICE_DOCUMENT_HISTORY_LAYER,
    )


def invoice_numbering_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261113_0099_invoice_numbering_guard.py"
        )
    )
    return _with_dependent_migration(
        frozen,
        invoice_snapshot_migration,
        _INVOICE_NUMBERING_ROUNDTRIP,
        detached=_INVOICE_SNAPSHOT_HISTORY_LAYER,
    )


def invoice_reservation_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261113_0098_invoice_reservations.py"
        )
    )
    numbered = _with_dependent_migration(
        frozen,
        invoice_numbering_migration,
        _INVOICE_RESERVATION_ROUNDTRIP,
        detached=_INVOICE_NUMBERING_HISTORY_LAYER,
    )

    # The new table depends on 0098 storage, not the later standalone guard probes.
    with_completion = _with_dependent_migration(
        numbered, invoice_completion_migration, _INVOICE_COMPLETION_ROUNDTRIP
    )
    return _with_dependent_migration(
        with_completion,
        invoice_activation_migration,
        _INVOICE_ACTIVATION_ROUNDTRIP,
        detached=_INVOICE_CLOSED_LAYER,
    )


def invoice_profile_migration():
    frozen = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "services/api/migrations/versions/20261113_0097_invoice_profiles.py"
        )
    )
    return _with_dependent_migration(
        frozen, invoice_reservation_migration, _INVOICE_PROFILE_ROUNDTRIP
    )


def restore_current_change_types(engine):
    # Isolated frozen OCR migrations replace the check with their historical type list.
    # Reinstate the current additive list only after the original probe completes.
    with engine.begin() as connection:
        _restore_current_change_types(connection)


def _restore_current_change_types(connection):
    entity_types = invoice_completion_migration()["_ENTITY_TYPES"]
    if connection.scalar(text("SELECT to_regclass('public.interledger_groups') IS NOT NULL")):
        entity_types += ", 'interledger_settings', 'interledger_groups'"
    if connection.scalar(text("SELECT to_regclass('public.manual_prices') IS NOT NULL")):
        entity_types += ", 'manual_prices'"
    if connection.scalar(
        text("SELECT to_regclass('public.reminder_email_deliveries') IS NOT NULL")
    ):
        entity_types += ", 'reminder_email_deliveries', 'reminder_email_evidence'"
    if connection.scalar(text("SELECT to_regclass('public.quote_observations') IS NOT NULL")):
        entity_types += ", 'quote_observations'"
    if connection.scalar(
        text("SELECT to_regclass('public.reminder_inbox_deliveries') IS NOT NULL")
    ):
        entity_types += ", 'reminder_inbox_deliveries'"
    connection.exec_driver_sql("ALTER TABLE change_log DROP CONSTRAINT ck_change_log_entity_type")
    connection.exec_driver_sql(
        "ALTER TABLE change_log ADD CONSTRAINT ck_change_log_entity_type CHECK "
        f"(entity_type IN ({entity_types}))"
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
            | credit_storage_migration()["_CHANGE_TRIGGER_SQL"]
            | credit_revision_migration()["_CHANGE_TRIGGER_SQL"]
            | credit_revision_member_migration()["_CHANGE_TRIGGER_SQL"]
            | refund_revision_header_migration()["_CHANGE_TRIGGER_SQL"]
            | refund_revision_evidence_migration()["_CHANGE_TRIGGER_SQL"]
            | return_revision_header_migration()["_CHANGE_TRIGGER_SQL"]
            | return_revision_member_migration()["_CHANGE_TRIGGER_SQL"]
            | document_revision_header_migration()["_CHANGE_TRIGGER_SQL"]
            | document_revision_line_migration()["_CHANGE_TRIGGER_SQL"]
            | document_accrual_member_migration()["_CHANGE_TRIGGER_SQL"]
            | invoice_profile_migration()["_CHANGE_TRIGGER_SQL"]
            | invoice_reservation_migration()["_CHANGE_TRIGGER_SQL"]
            | invoice_completion_migration()["_CHANGE_TRIGGER_SQL"]
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
        assert registered.get("trg_business_return_group_change_log") == "business_return_revisions"
        assert registered.get("trg_business_credit_group_change_log") == "business_credit_revisions"
        assert (
            registered.get("trg_business_refund_revision_change_log") == "business_refund_revisions"
        )
        if connection.scalar(text("SELECT to_regclass('public.interledger_groups') IS NOT NULL")):
            assert registered.get("tr_interledger_settings_change") == "interledger_settings"
            assert registered.get("tr_interledger_groups_change") == "interledger_groups"
        if connection.scalar(text("SELECT to_regclass('public.manual_prices') IS NOT NULL")):
            assert registered.get("tr_manual_price_change") == "manual_prices"
        if connection.scalar(
            text("SELECT to_regclass('public.reminder_email_deliveries') IS NOT NULL")
        ):
            assert (
                registered.get("tr_reminder_email_delivery_change") == "reminder_email_deliveries"
            )
            assert registered.get("tr_reminder_email_evidence_change") == "reminder_email_evidence"
        if connection.scalar(text("SELECT to_regclass('public.quote_observations') IS NOT NULL")):
            assert registered.get("tr_quote_observation_change") == "quote_observations"
        if connection.scalar(
            text("SELECT to_regclass('public.reminder_inbox_deliveries') IS NOT NULL")
        ):
            assert registered.get("tr_reminder_inbox_change") == "reminder_inbox_deliveries"


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
            # Keep foreign-key-connected tables together and skip completely empty groups.
            truncate_groups = (
                "change_log",
                "quote_observations",
                "manual_prices, manual_price_audits",
                "interledger_settings",
                "sync_financial_conflict_resolutions, sync_financial_pushes, "
                "sync_page_acknowledgements, sync_device_revisions, "
                "file_upload_chunks, file_upload_resumes, "
                "interledger_groups, ocr_confirmations, ocr_drafts, ocr_jobs, file_uploads, "
                "operation_file_links, stored_files, business_postings, business_confirmations, "
                "business_document_accruals, business_document_revision_lines, "
                "business_document_revisions, "
                "business_return_revision_members, business_return_revisions, "
                "business_refund_revision_returns, "
                "business_refund_revisions, "
                "business_credit_revision_members, business_credit_revisions, "
                "business_credit_lines, business_credits, "
                "business_refund_returns, business_refunds, "
                "business_allocation_return_lines, business_allocation_returns, "
                "business_allocation_lines, business_allocations, business_payments, "
                "recurring_invoice_instances, recurring_invoice_rules, "
                "command_receipts, opening_positions, journal_lines, journals, "
                "financial_operations, account_assets, accounts, business_document_lines, "
                "business_invoice_completions, "
                "business_invoice_reservations, "
                "business_invoice_profiles, "
                "business_documents, business_parties, business_projects",
                "reminder_inbox_deliveries, reminder_email_evidence, reminder_email_deliveries, "
                "reminder_delivery_revisions, reminder_delivery_configs, "
                "reminder_event_revisions, reminder_events",
                "ledger_period_receipts, ledger_period_audits, ledger_periods",
            )
            # Protect the emptiness checks with the same locks held by the original cleanup.
            connection.exec_driver_sql(
                f"LOCK TABLE {', '.join(truncate_groups)} IN ACCESS EXCLUSIVE MODE"
            )
            probes = []
            for group in truncate_groups:
                rows = " UNION ALL ".join(f"SELECT 1 FROM {table}" for table in group.split(", "))
                probes.append(f"EXISTS ({rows})")
            occupied = connection.exec_driver_sql(f"SELECT {', '.join(probes)}").one()
            tables = [
                group for group, has_rows in zip(truncate_groups, occupied, strict=True) if has_rows
            ]
            if tables:
                # RESTRICT continues to reject unexpected dependants of every truncated table.
                connection.exec_driver_sql(f"TRUNCATE TABLE {', '.join(tables)} RESTRICT")
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
            connection.execute(delete(AuthSession))
            connection.execute(delete(Administrator))
            # No owner remains: the normal asset change trigger ignores these bootstrap updates.
            # The initial cleanup already removed every stored notification.
            connection.execute(
                update(AssetRecord)
                .where((AssetRecord.enabled.is_(False)) | (AssetRecord.version != 1))
                .values(enabled=True, version=1)
            )
            if connection.scalar(text("SELECT EXISTS (SELECT 1 FROM change_log)")):
                raise RuntimeError("Disposable fixture retained a change notification")
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
    credit_history = request.node.path.parent == Path(
        __file__
    ).parent / "business" and request.node.path.stem in {
        "test_credit_revision_storage",
        "test_credit_revision_member_storage",
        "test_credit_projections",
        "test_credit_fact_validation",
        "test_credit_group_transitions",
        "test_credit_revision_receipts",
        "test_credit_current_capacity",
        "test_credit_revision_guards",
    }
    refund_history = request.node.path.parent == Path(
        __file__
    ).parent / "business" and request.node.path.stem in {
        "test_refund_revision_headers",
        "test_refund_revision_evidence",
        "test_refund_principal_projection",
        "test_refund_current_capacity",
        "test_refund_posting_fact",
        "test_refund_historical_validation",
        "test_refund_revision_receipt",
        "test_refund_revision_guards",
    }
    # Keep the original closed-gate assertions at the real pre-activation layer.
    document_history = request.node.path.parent == Path(
        __file__
    ).parent / "business" and request.node.path.stem in {
        "test_document_revision_line_identities",
        "test_document_group_proof",
        "test_document_revision_line_facts",
        "test_document_accrual_posting_facts",
        "test_document_revision_receipt_seals",
        "test_confirmed_document_revision_headers",
        "test_credit_current_document_capacity",
        "test_credit_pinned_validation",
        "test_credit_bound_revision_receipts",
        "test_credit_bound_current_reads",
        "test_credit_document_origin",
        "test_document_accrual_operation_heads",
        "test_document_accrual_links",
        "test_credit_document_sources",
        "test_credit_source_query_plans",
        "test_document_dependency_proof",
    }
    invoice_history = (
        request.node.path == Path(__file__).parent / "business/test_invoice_reservation_storage.py"
    )
    invoice_token = None
    invoice_definitions = None
    snapshot_history = invoice_history or (
        request.node.path == Path(__file__).parent / "business/test_invoice_numbering_guard.py"
    )
    snapshot_token = None
    snapshot_definitions = None
    invoice_document_history = snapshot_history or (
        request.node.path == Path(__file__).parent / "business/test_invoice_snapshot_binding.py"
    )
    invoice_document_token = None
    invoice_document_definitions = None
    invoice_key_history = invoice_document_history or request.node.path in {
        Path(__file__).parent / "business/test_invoice_document_binding.py",
        Path(__file__).parent / "business/test_invoice_document_binding_races.py",
    }
    invoice_key_token = None
    invoice_key_definitions = None
    invoice_previous_history = invoice_key_history or (
        request.node.path == Path(__file__).parent / "business/test_invoice_command_key.py"
        # This fixture runs a real Alembic head -> 0008 -> head round trip. All
        # registered triggers must be present when Alembic starts the downgrade.
        and "legacy_v1_receipts" not in request.fixturenames
    )
    # Keep the original staged-guard tests at their frozen schema. Run all new
    # commit and replay cases, and the genuine legacy Alembic round trip, at head.
    invoice_closed = invoice_previous_history or (
        request.node.path.parent == Path(__file__).parent / "business"
        and (request.node.path.stem, request.node.originalname)
        in {
            (
                "test_invoice_predecessor",
                "test_first_reviewed_invoice_has_no_predecessor_and_still_cannot_commit",
            ),
            (
                "test_invoice_completions",
                "test_complete_bilingual_metadata_has_fixed_dto_and_owned_notification_but_cannot_commit",
            ),
            (
                "test_invoice_reservation_inputs",
                "test_transient_candidate_matches_frozen_independent_input_and_passes_immediate_guards",
            ),
            (
                "test_invoice_reservation_command",
                "test_two_equal_first_attempts_cannot_return_success_or_leave_rows_when_commit_gate_is_closed",
            ),
            (
                "test_invoice_metadata_finalize",
                "test_even_two_sealed_rows_do_not_bypass_the_closed_reservation_commit",
            ),
            (
                "test_invoice_issuance_service",
                "test_closed_real_reservation_never_reaches_rendering_or_writes_a_false_receipt",
            ),
        }
    )
    invoice_closed_token = None
    invoice_previous_token = None
    invoice_previous_definitions = None
    document_token = None
    document_definitions = None
    refund_token = None
    refund_definitions = None
    credit_token = None
    credit_definitions = None
    try:
        change_trigger_registration(database.engine)
        cleanup()
        if document_history or refund_history or credit_history or invoice_closed:
            from tests.integration.business.test_credit_revision_storage import definitions
        if invoice_closed:
            with (
                database.engine.begin() as connection,
                Operations.context(MigrationContext.configure(connection)),
            ):
                active_definitions = definitions(connection)
                invoice_activation_migration()["downgrade"]()
            invoice_closed_token = _INVOICE_CLOSED_LAYER.set(True)
        if invoice_previous_history:
            with (
                database.engine.begin() as connection,
                Operations.context(MigrationContext.configure(connection)),
            ):
                invoice_previous_definitions = definitions(connection)
                invoice_predecessor_migration()["downgrade"]()
            invoice_previous_token = _INVOICE_PREVIOUS_HISTORY_LAYER.set(True)
        if invoice_key_history:
            with (
                database.engine.begin() as connection,
                Operations.context(MigrationContext.configure(connection)),
            ):
                invoice_key_definitions = definitions(connection)
                invoice_key_migration()["downgrade"]()
            invoice_key_token = _INVOICE_KEY_HISTORY_LAYER.set(True)
        if invoice_document_history:
            with (
                database.engine.begin() as connection,
                Operations.context(MigrationContext.configure(connection)),
            ):
                invoice_document_definitions = definitions(connection)
                invoice_document_migration()["downgrade"]()
            invoice_document_token = _INVOICE_DOCUMENT_HISTORY_LAYER.set(True)
        if snapshot_history:
            with (
                database.engine.begin() as connection,
                Operations.context(MigrationContext.configure(connection)),
            ):
                snapshot_definitions = definitions(connection)
                invoice_snapshot_migration()["downgrade"]()
            snapshot_token = _INVOICE_SNAPSHOT_HISTORY_LAYER.set(True)
        if invoice_history:
            with (
                database.engine.begin() as connection,
                Operations.context(MigrationContext.configure(connection)),
            ):
                invoice_definitions = definitions(connection)
                invoice_numbering_migration()["downgrade"]()
            invoice_token = _INVOICE_NUMBERING_HISTORY_LAYER.set(True)
        if document_history:
            with (
                database.engine.begin() as connection,
                Operations.context(MigrationContext.configure(connection)),
            ):
                document_definitions = definitions(connection)
                document_lifecycle_migration()["downgrade"]()
            document_token = _DOCUMENT_HISTORY_LAYER.set(True)
        if refund_history or credit_history:
            with (
                database.engine.begin() as connection,
                Operations.context(MigrationContext.configure(connection)),
            ):
                assert MigrationContext.configure(connection).get_current_heads() == _CURRENT_HEADS
                refund_definitions = definitions(connection)
                refund_lifecycle_migration()["downgrade"]()
            refund_token = _REFUND_HISTORY_LAYER.set(True)
        if credit_history:
            with (
                database.engine.begin() as connection,
                Operations.context(MigrationContext.configure(connection)),
            ):
                assert MigrationContext.configure(connection).get_current_heads() == _CURRENT_HEADS
                credit_definitions = definitions(connection)
                refund_current_capacity_migration()["downgrade"]()
                credit_lifecycle_migration()["downgrade"]()
            credit_token = _CREDIT_HISTORY_LAYER.set(True)
        owner = create_admin(
            database.engine, "structure-test-admin", "fictional-structure-password-2026"
        )
        if file_round_trip:
            # Real FKs prevent independently dropping 0008's files even with empty OCR tables.
            # The frozen downgrade refuses OCR rows/history; no CASCADE or guard bypass occurs.
            with database.engine.begin() as c, Operations.context(MigrationContext.configure(c)):
                invoice_profile_migration()["downgrade"]()
            isolated_ocr_schema(database.engine, "downgrade")
            ocr_detached = True
        elif confirmation_probe:
            # Preserve the exact frozen 0013 probe, including its RESTRICT and trigger-count checks.
            isolated_confirmation_schema(database.engine, "downgrade")
            confirmation_detached = True
        yield database.engine, owner
    finally:
        if credit_token is not None:
            try:
                with (
                    database.engine.begin() as connection,
                    Operations.context(MigrationContext.configure(connection)),
                ):
                    credit_lifecycle_migration()["upgrade"]()
                    refund_current_capacity_migration()["upgrade"]()
                    assert (
                        MigrationContext.configure(connection).get_current_heads() == _CURRENT_HEADS
                    )
                    assert definitions(connection) == credit_definitions
            finally:
                _CREDIT_HISTORY_LAYER.reset(credit_token)
        if refund_token is not None:
            _REFUND_HISTORY_LAYER.reset(refund_token)
            with (
                database.engine.begin() as connection,
                Operations.context(MigrationContext.configure(connection)),
            ):
                refund_lifecycle_migration()["upgrade"]()
                assert MigrationContext.configure(connection).get_current_heads() == _CURRENT_HEADS
                assert definitions(connection) == refund_definitions
        if ocr_detached:
            isolated_ocr_schema(database.engine, "upgrade")
            with database.engine.begin() as c, Operations.context(MigrationContext.configure(c)):
                invoice_profile_migration()["upgrade"]()
        elif confirmation_detached:
            isolated_confirmation_schema(database.engine, "upgrade")
        if ocr_detached:
            restore_current_change_types(database.engine)
        change_trigger_registration(database.engine, repair_recreated_file_tables=file_round_trip)
        cleanup()
        if document_token is not None:
            _DOCUMENT_HISTORY_LAYER.reset(document_token)
            with (
                database.engine.begin() as connection,
                Operations.context(MigrationContext.configure(connection)),
            ):
                document_lifecycle_migration()["upgrade"]()
                assert definitions(connection) == document_definitions
        if invoice_token is not None:
            _INVOICE_NUMBERING_HISTORY_LAYER.reset(invoice_token)
            with (
                database.engine.begin() as connection,
                Operations.context(MigrationContext.configure(connection)),
            ):
                invoice_numbering_migration()["upgrade"]()
                assert definitions(connection) == invoice_definitions
        if snapshot_token is not None:
            _INVOICE_SNAPSHOT_HISTORY_LAYER.reset(snapshot_token)
            with (
                database.engine.begin() as connection,
                Operations.context(MigrationContext.configure(connection)),
            ):
                invoice_snapshot_migration()["upgrade"]()
                assert definitions(connection) == snapshot_definitions
        if invoice_document_token is not None:
            _INVOICE_DOCUMENT_HISTORY_LAYER.reset(invoice_document_token)
            with (
                database.engine.begin() as connection,
                Operations.context(MigrationContext.configure(connection)),
            ):
                invoice_document_migration()["upgrade"]()
                assert definitions(connection) == invoice_document_definitions
        if invoice_key_token is not None:
            _INVOICE_KEY_HISTORY_LAYER.reset(invoice_key_token)
            with (
                database.engine.begin() as connection,
                Operations.context(MigrationContext.configure(connection)),
            ):
                invoice_key_migration()["upgrade"]()
                assert definitions(connection) == invoice_key_definitions
        if invoice_previous_token is not None:
            _INVOICE_PREVIOUS_HISTORY_LAYER.reset(invoice_previous_token)
            with (
                database.engine.begin() as connection,
                Operations.context(MigrationContext.configure(connection)),
            ):
                invoice_predecessor_migration()["upgrade"]()
                assert definitions(connection) == invoice_previous_definitions
        if invoice_closed_token is not None:
            _INVOICE_CLOSED_LAYER.reset(invoice_closed_token)
            with (
                database.engine.begin() as connection,
                Operations.context(MigrationContext.configure(connection)),
            ):
                invoice_activation_migration()["upgrade"]()
                assert definitions(connection) == active_definitions
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
