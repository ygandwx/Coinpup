"""Downgrade retains the exact previously deployed draft guard."""

import runpy
from pathlib import Path


def test_frozen_downgrade_body_matches_0013_without_runtime_imports():
    versions = Path(__file__).resolve().parents[3] / "services/api/migrations/versions"
    old = runpy.run_path(str(versions / "20261004_0013_ocr_jobs_and_drafts.py"))
    current = runpy.run_path(str(versions / "20261008_0014_ocr_confirmations.py"))
    assert current["_OLD_DRAFT_GUARD_SQL"] == old["_DRAFT_GUARD_SQL"]
