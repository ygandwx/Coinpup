"""Exercise the CI plugin in actual pytest subprocesses on disposable fictional modules."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.db_ci_manifest import check_part, partition_for

ROOT = Path(__file__).resolve().parents[3]
SHA = "a3" * 20


def suite(directory):
    modules = directory / "tests/integration"
    modules.mkdir(parents=True)
    (directory / "pytest.ini").write_text(
        "[pytest]\nmarkers = integration: fictional database case\n"
    )
    for module in ("test_first", "test_other"):
        (modules / f"{module}.py").write_text(
            "import pytest\n"
            "from pathlib import Path\n"
            "@pytest.mark.integration\n"
            "@pytest.mark.parametrize('item', ['one', 'two'])\n"
            "def test_fictional(item):\n"
            "    with Path('trace.txt').open('a') as f:\n"
            f"        print('{module}', item, file=f)\n"
            "def test_outside_database_selection():\n"
            "    raise AssertionError('unmarked test ran')\n",
            encoding="utf-8",
        )
    return modules


def run_part(directory, part, *extra):
    destination = directory / f"part-{part}.json"
    env = os.environ.copy()
    env.update(
        PYTHONPATH=str(ROOT),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTEST_ADDOPTS="",
        GITHUB_SHA=SHA,
        COINPUP_DB_CI_PART=str(part),
        COINPUP_DB_CI_REPORT=str(destination),
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "scripts.db_ci_pytest",
            "tests/integration",
            "-m",
            "integration",
            "-q",
            *extra,
        ],
        cwd=directory,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, destination


def aggregate(paths, sha=SHA):
    # -S excludes site packages including pytest on this aggregate-only entry point.
    return subprocess.run(
        [sys.executable, "-S", "-m", "scripts.db_ci_manifest", "verify", *map(str, paths)],
        cwd=ROOT,
        env=dict(os.environ, GITHUB_SHA=sha, PYTHONPATH=str(ROOT)),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def good(tmp_path_factory):
    directory = tmp_path_factory.mktemp("fictional_db_partitions")
    suite(directory)
    paths = []
    for part in (0, 1):
        result, path = run_part(directory, part)
        assert result.returncode == 0, result.stdout + result.stderr
        paths.append(path)
    return directory, paths


def test_two_real_runs_execute_each_selected_module_in_order_and_verify_without_pytest(good):
    directory, paths = good
    assert (directory / "trace.txt").read_text().splitlines() == [
        "test_first one",
        "test_first two",
        "test_other one",
        "test_other two",
    ]
    prefix = "tests/integration/"
    expected = [
        f"{prefix}{module}.py::test_fictional[{item}]"
        for module in ("test_first", "test_other")
        for item in ("one", "two")
    ]
    for i, path in enumerate(paths):
        report = json.loads(path.read_text())
        assert report["manifest"] == expected
        selected = check_part(report, i, SHA)
        assert selected == expected[2 * i : 2 * i + 2]
        assert report["events"] == [
            [node, phase, "passed"] for node in selected for phase in ("setup", "call", "teardown")
        ]
    result = aggregate(paths)
    assert result.returncode == 0, result.stderr
    assert "4 passed (part 0: 2, part 1: 2)" in result.stdout
    assert SHA in result.stdout


@pytest.mark.parametrize(
    "bad_body",
    [
        'def test_bad(): pytest.skip("fictional unavailable")',
        '@pytest.mark.xfail(reason="fictional")\ndef test_bad(): assert False',
        '@pytest.mark.xfail(reason="fictional")\ndef test_bad(): pass',
        "def test_bad(): assert False",
        '@pytest.fixture\ndef broken(): raise ValueError("fictional setup")\n'
        "def test_bad(broken): pass",
        '@pytest.fixture\ndef broken():\n    yield\n    raise ValueError("fictional teardown")\n'
        "def test_bad(broken): pass",
    ],
)
def test_real_skips_xfails_xpasses_and_failures_all_reject_the_part(tmp_path, bad_body):
    modules = suite(tmp_path)
    (modules / "test_first.py").write_text(
        "import pytest\npytestmark = pytest.mark.integration\n" + bad_body
    )
    result, path = run_part(tmp_path, 0)
    assert result.returncode != 0
    assert "DB check evidence rejected" in result.stdout
    report = json.loads(path.read_text())
    assert any(outcome != "passed" for _, _, outcome in report["events"])
    with pytest.raises(ValueError):
        check_part(report, 0, SHA)


def test_collection_skip_and_additional_deselection_cannot_silently_shrink_both_manifests(tmp_path):
    modules = suite(tmp_path)
    (modules / "test_hidden.py").write_text(
        "import pytest\npytest.skip('fictional module unavailable', allow_module_level=True)\n"
    )
    result, path = run_part(tmp_path, 0)
    assert result.returncode != 0
    report = json.loads(path.read_text())
    assert report["collection_errors"] == [["tests/integration/test_hidden.py", "skipped"]]
    with pytest.raises(ValueError, match="collection failure or skip"):
        check_part(report, 0, SHA)
    filtered = tmp_path / "filtered"
    suite(filtered)
    result, _ = run_part(filtered, 0, "-k", "one")
    assert result.returncode != 0
    assert "Additional pytest selection changed" in result.stdout + result.stderr


@pytest.mark.parametrize(
    "fault",
    [
        "missing test",
        "duplicate test",
        "wrong manifest",
        "reordered manifest",
        "missing teardown",
        "repeated setup",
        "wrong order",
        "unexpected test",
        "failed call",
        "status",
        "wrong sha",
        "wrong part",
    ],
)
def test_aggregate_rejects_missing_duplicate_and_mismatched_evidence(tmp_path, good, fault):
    _, paths = good
    report = json.loads(paths[0].read_text())
    manifest, events = report["manifest"], report["events"]
    if fault == "missing test":
        del manifest[0]
        del events[:3]
    elif fault == "duplicate test":
        manifest.append(manifest[0])
    elif fault == "wrong manifest":
        manifest[-1] = manifest[-1].replace("two", "other")
    elif fault == "reordered manifest":
        manifest[2:] = reversed(manifest[2:])
    elif fault == "missing teardown":
        del events[2]
    elif fault == "repeated setup":
        events[1] = events[0]
    elif fault == "wrong order":
        events[:] = events[3:] + events[:3]
    elif fault == "unexpected test":
        events[1][0] = manifest[-1]
    elif fault == "failed call":
        events[1][2] = "skipped"
    elif fault == "status":
        report["exitstatus"] = 1
    elif fault == "wrong sha":
        report["sha"] = "b4" * 20
    else:
        report["part"] = 1
    altered = tmp_path / "altered.json"
    altered.write_text(json.dumps(report))
    result = aggregate([altered, paths[1]])
    assert result.returncode != 0
    assert "DB check evidence rejected" in result.stderr


def test_evidence_requires_both_distinct_files_at_the_expected_sha(good, tmp_path):
    _, paths = good
    for invalid in ([paths[0], paths[0]], list(reversed(paths)), [paths[0], tmp_path / "missing"]):
        assert aggregate(invalid).returncode != 0
    assert aggregate(paths, sha="").returncode != 0
    assert aggregate(paths, sha="b4" * 20).returncode != 0


def test_partition_uses_module_sha256_low_bit_and_never_param_or_class_names():
    for path, expected in [("test_first", 0), ("test_other", 1)]:
        for suffix in ["test_short", "TestGroup::test_case[with::param]", "test_param[ü]"]:
            assert partition_for(f"tests/integration/{path}.py::{suffix}") == expected
    for invalid in [
        "",
        "/tmp/test_x.py::x",
        "../tests/integration/x.py::x",
        "tests/integration/../unit/x.py::x",
        "tests\\integration\\x.py::x",
    ]:
        with pytest.raises(ValueError):
            partition_for(invalid)


def test_slow_diagnostic_reports_the_live_case_without_interrupting_or_losing_phases(tmp_path):
    modules = suite(tmp_path)
    (tmp_path / "conftest.py").write_text(
        "from scripts import db_ci_pytest\ndb_ci_pytest.SLOW_TEST_SECONDS = 0.05\n"
    )
    file = modules / "test_first.py"
    file.write_text(
        "import pytest, time\n@pytest.mark.integration\n"
        "def test_slow(): time.sleep(0.25)\n"
        "@pytest.mark.integration\ndef test_fast(): pass\n"
    )
    node = "tests/integration/test_first.py::test_slow"
    part = partition_for(node)
    result, destination = run_part(tmp_path, part)
    assert result.returncode == 0, result.stderr
    assert f"Slow PostgreSQL test after 0.05s: {node}" in result.stderr
    assert "in test_slow" in result.stderr
    check_part(json.loads(destination.read_text()), part, SHA)
