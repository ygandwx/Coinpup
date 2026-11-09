"""Opt-in CI plugin for the unchanged tests/integration -m integration selection."""

import faulthandler
import json
import os
import sys
from pathlib import Path
from threading import Timer

import pytest

from scripts.db_ci_manifest import check_part, github_sha, partition_for

SLOW_TEST_SECONDS = 60


def pytest_configure(config):
    part = os.environ.get("COINPUP_DB_CI_PART")
    destination = os.environ.get("COINPUP_DB_CI_REPORT")
    if part not in ("0", "1") or not destination:
        raise pytest.UsageError("Set COINPUP_DB_CI_PART (0 or 1) and COINPUP_DB_CI_REPORT")
    if config.args != ["tests/integration"] or config.option.markexpr != "integration":
        raise pytest.UsageError(
            "DB partitions require the full tests/integration -m integration selection"
        )
    if config.option.ignore or config.option.ignore_glob or config.option.collectonly:
        raise pytest.UsageError("DB partitions require full collection and execution")
    try:
        sha = github_sha()
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from exc
    path = Path(destination)
    if path.exists():
        raise pytest.UsageError(f"DB check report already exists: {path}")
    # Keep the original stderr across per-test fd capture.
    diagnostic = os.fdopen(os.dup(sys.__stderr__.fileno()), "w", encoding="utf-8")
    config.add_cleanup(diagnostic.close)
    config.pluginmanager.register(DatabasePartition(int(part), sha, path, diagnostic))


class DatabasePartition:
    def __init__(self, part, sha, destination, diagnostic):
        self.part = part
        self.sha = sha
        self.destination = destination
        self.diagnostic = diagnostic
        self.manifest = []
        self.events = []
        self.collection_errors = []

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(self, config, items):
        # Other hooks include pytest's normal -m selection. Reject any extra filtering or reorder.
        marked = [
            item.nodeid for item in items if item.get_closest_marker("integration") is not None
        ]
        yield
        self.manifest = [item.nodeid for item in items]
        if self.manifest != marked:
            raise pytest.UsageError("Additional pytest selection changed the integration manifest")
        if len(self.manifest) != len(set(self.manifest)):
            raise pytest.UsageError("Duplicate integration node IDs in the complete collection")
        keep, other = [], []
        for item in items:
            (keep if partition_for(item.nodeid) == self.part else other).append(item)
        config.hook.pytest_deselected(items=other)
        items[:] = keep

    def dump_slow_test(self, nodeid):
        print(
            f"Slow PostgreSQL test after {SLOW_TEST_SECONDS}s: {nodeid}",
            file=self.diagnostic,
            flush=True,
        )
        # A Python thread holds the GIL during synchronous frame traversal on CPython 3.12.
        faulthandler.dump_traceback(file=self.diagnostic, all_threads=True)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item):
        timer = Timer(SLOW_TEST_SECONDS, self.dump_slow_test, (item.nodeid,))
        timer.start()
        try:
            return (yield)
        finally:
            timer.cancel()
            timer.join()

    def pytest_collectreport(self, report):
        if not report.passed:
            self.collection_errors.append([report.nodeid, report.outcome])

    def pytest_runtest_logreport(self, report):
        outcome = "xfail/xpass" if hasattr(report, "wasxfail") else report.outcome
        self.events.append([report.nodeid, report.when, outcome])

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        report = {
            "sha": self.sha,
            "part": self.part,
            "manifest": self.manifest,
            "events": self.events,
            "collection_errors": self.collection_errors,
            "exitstatus": int(exitstatus),
        }
        try:
            with self.destination.open("x", encoding="utf-8") as output:
                json.dump(report, output, ensure_ascii=True, separators=(",", ":"))
            check_part(report, self.part, self.sha)
        except (OSError, ValueError) as exc:
            terminal = session.config.pluginmanager.get_plugin("terminalreporter")
            if terminal is not None:
                terminal.write_line(f"DB check evidence rejected: {exc}", red=True)
            if session.exitstatus == pytest.ExitCode.OK:
                session.exitstatus = pytest.ExitCode.TESTS_FAILED
