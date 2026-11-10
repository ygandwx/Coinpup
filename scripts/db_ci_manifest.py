"""Verify two independent PostgreSQL pytest runs using only the standard library."""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath

# Full 200-line corrections are expensive on real PostgreSQL. Keep these two modules
# apart from the current-group and current-HTTP read suites that hash to partition 1.
_PART_ZERO_MODULES = {
    "tests/integration/business/test_credit_revision_boundaries.py",
    "tests/integration/business/test_credit_revision_history.py",
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
    if module in _PART_ZERO_MODULES:
        return 0
    return hashlib.sha256(module.encode("utf-8")).digest()[-1] & 1


def check_part(report, part, sha):
    """Derive this runner's assignment from its full collection, then prove each phase."""
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
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    first, second = [check_part(report, i, sha) for i, report in enumerate(reports)]
    manifest = reports[0]["manifest"]
    if manifest != reports[1]["manifest"]:
        raise ValueError("The two runners collected different integration manifests")
    if set(first) & set(second) or set(first + second) != set(manifest):
        raise ValueError("The two assignments overlap or omit integration tests")
    print(
        f"PostgreSQL integration: {len(manifest)} passed "
        f"(part 0: {len(first)}, part 1: {len(second)}) at {sha}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["verify"])
    parser.add_argument("reports", type=Path, nargs=2, metavar="PART.json")
    args = parser.parse_args()
    try:
        verify(args.reports)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"DB check evidence rejected: {exc}\n")


if __name__ == "__main__":
    main()
