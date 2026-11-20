"""Verify all independent PostgreSQL pytest runs using only the standard library."""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath

# Full 2567-case native runs measured 134 minutes total; keep complete modules together.
PART_COUNT = 5
_HEAVY_MODULE_PARTS = {
    "tests/integration/business/test_allocation_return_revision_receipts.py": 1,
    "tests/integration/business/test_credit_projections.py": 3,
    "tests/integration/business/test_credit_revision_boundaries.py": 2,
    "tests/integration/business/test_credit_revision_current.py": 1,
    "tests/integration/business/test_credit_revision_history.py": 0,
    "tests/integration/business/test_credit_revision_lifecycle.py": 0,
    "tests/integration/business/test_credit_revision_reads.py": 2,
    "tests/integration/business/test_credit_service.py": 0,
    "tests/integration/business/test_credit_versioned_corrections.py": 3,
    "tests/integration/business/test_credit_versioned_create.py": 1,
    "tests/integration/business/test_current_allocation_return_members.py": 2,
    "tests/integration/business/test_document_allocation_native.py": 4,
    "tests/integration/business/test_return_command_current.py": 2,
    "tests/integration/business/test_return_complete_lifecycle.py": 4,
    "tests/integration/business/test_return_current_http.py": 3,
    "tests/integration/business/test_return_revision_history_http.py": 0,
    "tests/integration/business/test_return_revision_service.py": 2,
    "tests/integration/business/test_return_source_history.py": 1,
    "tests/integration/business/test_settlement_service.py": 4,
    "tests/integration/ocr/test_payment_confirmation_schema.py": 4,
    "tests/integration/ocr/test_payment_confirmation_service.py": 3,
    "tests/integration/ocr/test_payment_evidence_reads.py": 0,
}


def github_sha():
    sha = os.environ.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("A full GITHUB_SHA is required for DB check evidence")
    return sha


def partition_for(nodeid):
    module, separator, test = nodeid.partition("::")
    path = PurePosixPath(module)
    if (
        not separator
        or not test
        or path.parts[:2] != ("tests", "integration")
        or path.suffix != ".py"
        or str(path) != module
        or "\\" in module
        or ".." in path.parts
    ):
        raise ValueError(f"Invalid repository-relative integration node ID: {nodeid!r}")
    if module in _HEAVY_MODULE_PARTS:
        return _HEAVY_MODULE_PARTS[module]
    return hashlib.sha256(module.encode("utf-8")).digest()[-1] % PART_COUNT


def check_part(report, part, sha):
    """Derive this runner's assignment from its full collection, then prove each phase."""
    if type(part) is not int or part not in range(PART_COUNT):
        raise ValueError("Unexpected database partition")
    if not isinstance(report, dict) or report.get("sha") != sha:
        raise ValueError(f"Part {part} has an unexpected GITHUB_SHA")
    if type(report.get("part")) is not int or report["part"] != part:
        raise ValueError(f"Expected evidence from part {part}")
    manifest = report.get("manifest")
    if (
        not isinstance(manifest, list)
        or not manifest
        or not all(isinstance(n, str) for n in manifest)
    ):
        raise ValueError(f"Part {part} has no complete integration collection")
    if len(manifest) != len(set(manifest)):
        raise ValueError(f"Part {part} collected duplicate integration node IDs")
    selected = [node for node in manifest if partition_for(node) == part]
    if not selected:
        raise ValueError(f"Part {part} did not collect any assigned tests")
    if report.get("collection_errors") != []:
        raise ValueError(f"Part {part} had a collection failure or skip")
    if type(report.get("exitstatus")) is not int or report["exitstatus"] != 0:
        raise ValueError(f"Part {part} pytest exit status: {report.get('exitstatus')}")
    events = report.get("events")
    if not isinstance(events, list) or len(events) != len(selected) * 3:
        raise ValueError(f"Part {part} has missing or extra test phase reports")
    for index, node in enumerate(selected):
        for offset, phase in enumerate(("setup", "call", "teardown")):
            if events[3 * index + offset] != [node, phase, "passed"]:
                raise ValueError(f"Part {part} expected one passing {phase} for {node}")
    return selected


def verify(paths):
    sha = github_sha()
    if len(paths) != PART_COUNT:
        raise ValueError(f"Require exactly {PART_COUNT} database partition reports")
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    assignments = [check_part(report, i, sha) for i, report in enumerate(reports)]
    manifest = reports[0]["manifest"]
    if any(report["manifest"] != manifest for report in reports[1:]):
        raise ValueError("The runners collected different integration manifests")
    executed = [node for selected in assignments for node in selected]
    if len(executed) != len(set(executed)) or set(executed) != set(manifest):
        raise ValueError("The assignments overlap or omit integration tests")
    sizes = ", ".join(f"part {i}: {len(selected)}" for i, selected in enumerate(assignments))
    print(f"PostgreSQL integration: {len(manifest)} passed ({sizes}) at {sha}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["verify"])
    parser.add_argument("reports", type=Path, nargs=PART_COUNT, metavar="PART.json")
    args = parser.parse_args()
    try:
        verify(args.reports)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"DB check evidence rejected: {exc}\n")


if __name__ == "__main__":
    main()
