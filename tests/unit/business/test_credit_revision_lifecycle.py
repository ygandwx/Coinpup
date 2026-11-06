"""Lifecycle replacement freezes old definitions and leaves the core history graph intact."""

import runpy
from pathlib import Path


def test_lifecycle_restores_exact_frozen_bodies_without_replacing_the_core_history():
    versions = Path(__file__).resolve().parents[3] / "services/api/migrations/versions"
    migration = runpy.run_path(str(versions / "20261106_0048_credit_revision_lifecycle.py"))
    capacity = runpy.run_path(str(versions / "20261106_0046_credit_current_capacity.py"))
    initial = runpy.run_path(str(versions / "20261021_0039_business_credit_validation.py"))
    gate = runpy.run_path(str(versions / "20261106_0040_credit_revision_headers.py"))
    assert migration["_OLD_GROUP"] == capacity["_CREDIT"]
    assert (
        migration["_OLD_SHAPE"].replace("CREATE OR REPLACE FUNCTION", "CREATE FUNCTION")
        == "\nCREATE FUNCTION coinpup_validate_shape_credit"
        + initial["_VALIDATION"].split("CREATE FUNCTION coinpup_validate_shape_credit", 1)[1]
    )
    assert (
        migration["_OLD_GATE"].replace("CREATE OR REPLACE FUNCTION", "CREATE FUNCTION")
        == "\nCREATE FUNCTION coinpup_validate_credit_revision"
        + gate["_GUARDS"]
        .split("CREATE FUNCTION coinpup_validate_credit_revision", 1)[1]
        .split("CREATE FUNCTION coinpup_check_credit_revision", 1)[0]
    )
    assert migration["_GROUP"].count("CREATE OR REPLACE FUNCTION") == 3
    assert not any(
        name in migration["_GROUP"]
        for name in (
            "coinpup_validate_operation_history",
            "coinpup_validate_initial_journal",
            "coinpup_validate_posting_shape",
            "coinpup_validate_reversal",
        )
    )
    assert migration["down_revision"] == "20261106_0047"
