"""Frozen shape SQL and migration boundaries complement real PostgreSQL constraint tests."""

import ast
import hashlib
import importlib.util
import io
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[3]
VERSIONS = ROOT / "services/api/migrations/versions"
LEGACY = VERSIONS / "20261003_0007_operation_revisions.py"
# Captured from published main 1091a07, rather than regenerated from new implementation.
LEGACY_SHA256 = "94c5fcee716a85675194e5eb7fa4bf1a0588699bd2755df98312c6098cefcfb6"
KINDS = ("opening", "income", "expense", "transfer", "exchange")
NEW_FUNCTIONS = {"coinpup_validate_journal_common"} | {
    "coinpup_validate_shape_" + kind for kind in KINDS
}
DISPATCHER = "coinpup_validate_posting_shape"


def legacy_shape():
    module = ast.parse(LEGACY.read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_POSTING_SHAPE_SQL"
            for target in node.targets
        )
    )
    return ast.literal_eval(assignment.value)


@pytest.fixture
def migration():
    paths = list(VERSIONS.glob("20261004_0011_*.py"))
    assert len(paths) == 1
    spec = importlib.util.spec_from_file_location("posting_shape_migration_under_test", paths[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def statements(migration, monkeypatch, direction):
    emitted = []
    monkeypatch.setattr(migration, "op", SimpleNamespace(execute=emitted.append))
    getattr(migration, direction)()
    return emitted


def compact(sql):
    return " ".join(sql.split())


def between(sql, start, end):
    return sql.split(start, 1)[1].split(end, 1)[0]


def upgrade_functions(migration, monkeypatch):
    return {
        re.match(r"\s*CREATE (?:OR REPLACE )?FUNCTION (\w+)\(", sql)[1]: sql
        for sql in statements(migration, monkeypatch, "upgrade")
    }


def test_revision_history_migration_is_frozen():
    contents = LEGACY.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(contents).hexdigest() == LEGACY_SHA256


def test_shape_migration_imports_no_application_code_and_has_one_head(migration):
    module = ast.parse(Path(migration.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom):
            assert node.module is not None and node.module.split(".")[0] in {
                "alembic",
                "sqlalchemy",
            }
        elif isinstance(node, ast.Import):
            assert all(
                alias.name.split(".")[0] in {"alembic", "sqlalchemy"} for alias in node.names
            )
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert len(scripts.get_heads()) == 1
    assert scripts.get_revision("20261004_0011").down_revision == "20261004_0010"


def test_upgrade_replaces_only_shape_dispatch_and_creates_six_helpers(migration, monkeypatch):
    sql = statements(migration, monkeypatch, "upgrade")
    created = []
    for statement in sql:
        match = re.match(r"\s*CREATE (OR REPLACE )?FUNCTION (\w+)\(", statement)
        assert match is not None
        created.append(match[2])
        assert "SET search_path = pg_catalog" in statement
        if match[2] == DISPATCHER:
            assert match[1] == "OR REPLACE "
        else:
            assert match[1] is None
    assert len(created) == len(set(created)) == 7
    assert set(created) == NEW_FUNCTIONS | {DISPATCHER}


def test_dispatcher_keeps_complete_identity_guard_before_common_and_each_kind(
    migration, monkeypatch
):
    sql = upgrade_functions(migration, monkeypatch)[DISPATCHER]
    identity = legacy_shape().split("SELECT * INTO header", 1)[1].split("SELECT count(*)", 1)[0]
    identity = "SELECT * INTO header" + identity
    assert compact(identity) in compact(sql)
    common_call = sql.index("public.coinpup_validate_journal_common(")
    assert sql.index("Posting journal identity is invalid") < common_call
    assert sql.count("public.coinpup_validate_journal_common(") == 1
    for kind in KINDS:
        call = "public.coinpup_validate_shape_" + kind + "("
        assert sql.count(call) == 1 and sql.index(call) > common_call
        assert f"WHEN '{kind}' THEN PERFORM {call}journal_identifier, common_result);" in compact(
            sql
        )
    fallback = "ELSE RAISE EXCEPTION USING ERRCODE = '23514',"
    assert fallback in compact(sql)
    assert "CONSTRAINT = 'ck_journal_shape'" in sql[sql.index("ELSE", common_call) :]


@pytest.mark.parametrize("kind", KINDS)
def test_kind_helpers_preserve_all_old_checks_around_deferred_common_error(
    migration, monkeypatch, kind
):
    old = legacy_shape()
    sql = compact(upgrade_functions(migration, monkeypatch)["coinpup_validate_shape_" + kind])
    for count in ("line_count", "account_count", "asset_count"):
        assert f"{count} := (common_result->>'{count}')::integer;" in sql
    if kind == "transfer":
        pre = between(
            old, "IF operation.kind = 'transfer' THEN", "ELSIF operation.kind = 'exchange'"
        )
    elif kind == "exchange":
        pre = between(old, "ELSIF operation.kind = 'exchange' THEN", "ELSIF line_count < 2")
    else:
        pre = "IF line_count < 2" + between(
            old, "ELSIF line_count < 2", "SELECT count(DISTINCT component_no)"
        )
    pre = compact(pre)
    assert pre in sql
    deferred = "IF common_result->>'error_message' IS NOT NULL THEN"
    assert deferred in sql and sql.index(pre) < sql.index(deferred)
    # No common error may become a generic shape error or lose its original SQLSTATE.
    assert "ERRCODE = common_result->>'error_sqlstate'" in sql
    assert "MESSAGE = common_result->>'error_message'" in sql
    assert "CONSTRAINT = common_result->>'error_constraint'" in sql
    if kind == "opening":
        post = between(old, "IF operation.kind = 'opening' THEN", "ELSE\n            IF EXISTS")
    else:
        post = between(old, "ELSE\n", "IF operation.kind = 'expense' AND EXISTS")
    post = compact(post)
    assert post in sql and sql.index(post) > sql.index(deferred)
    if kind in {"income", "expense"}:
        start = f"IF operation.kind = '{kind}' AND EXISTS"
        sign = start + between(old, start, "END IF;")
        sign += "END IF;"
        sign = compact(sign)
        assert sign in sql and sql.index(sign) > sql.index(post)


def test_common_preserves_original_counts_fee_predicates_balance_and_first_error(
    migration, monkeypatch
):
    old = legacy_shape()
    sql = compact(upgrade_functions(migration, monkeypatch)["coinpup_validate_journal_common"])
    counts = "SELECT count(*)" + between(old, "SELECT count(*)", "IF operation.kind = 'transfer'")
    fees = "SELECT count(DISTINCT component_no)" + between(
        old, "SELECT count(DISTINCT component_no)", "IF maximum_component"
    )
    assert compact(counts) in sql and compact(fees) in sql
    old_common = between(old, "IF maximum_component", "IF operation.kind = 'opening' THEN")
    conditions = re.findall(r"(?:^|END IF;\s*)\s*IF (.*?) THEN\s*RAISE EXCEPTION", old_common, re.S)
    # Include the first condition whose initial IF was consumed by the boundary marker.
    conditions.insert(0, "maximum_component" + old_common.split(" THEN", 1)[0])
    errors = re.findall(
        r"ERRCODE = '([^']+)',\s*MESSAGE = '([^']+)',\s*CONSTRAINT = '([^']+)'", old_common
    )
    assert len(conditions) == len(errors) == 3
    # The scalar operation_kind is loaded from the same header/operation join.
    # Rename only that reference; retain every comparison and complete EXISTS subquery.
    conditions = [
        compact(condition).replace("operation.kind", "operation_kind") for condition in conditions
    ]
    positions = [sql.index(condition) for condition in conditions]
    assert positions == sorted(positions)
    for index, (state, message, constraint) in enumerate(errors):
        assert sql[: positions[index]].endswith("IF " if index == 0 else "ELSIF ")
        end = (
            positions[index + 1]
            if index + 1 < len(positions)
            else sql.index("END IF;", positions[index])
        )
        block = sql[positions[index] : end]
        assert f"error_sqlstate := '{state}';" in block
        assert f"error_message := '{message}';" in block
        assert f"error_constraint := '{constraint}';" in block
    for field in (
        "line_count",
        "account_count",
        "asset_count",
        "error_sqlstate",
        "error_message",
        "error_constraint",
    ):
        assert f"'{field}', {field}" in sql
    assert "RAISE EXCEPTION" not in sql
    # A single IF/ELSIF chain records only the earliest failing common check.
    assert sql.count("ELSIF ") == 2


def test_downgrade_restores_exact_frozen_shape_before_dropping_helpers(migration, monkeypatch):
    sql = statements(migration, monkeypatch, "downgrade")
    expected = legacy_shape().replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    assert sql[0] == expected
    dropped = []
    for statement in sql[1:]:
        match = re.fullmatch(r"DROP FUNCTION (\w+)\(([^;]*)\);?", statement.strip())
        assert match is not None
        dropped.append(match[1])
        expected_parameters = (
            "uuid" if match[1] == "coinpup_validate_journal_common" else "uuid,jsonb"
        )
        assert match[2].replace(" ", "") == expected_parameters
    assert len(dropped) == len(set(dropped)) == 6
    assert set(dropped) == NEW_FUNCTIONS


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_shape_migration_emits_offline_postgresql_sql(migration, direction):
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        getattr(migration, direction)()
    sql = output.getvalue()
    assert "CREATE OR REPLACE FUNCTION " + DISPATCHER in sql
    assert "CREATE TABLE" not in sql and "ALTER TABLE" not in sql
    assert "TRIGGER" not in sql
