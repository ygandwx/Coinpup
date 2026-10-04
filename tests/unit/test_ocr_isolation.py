"""Real bounded processes use fictional stdlib probes, never an OCR recognizer."""

import json
import os
import shutil
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from coinpup_api.ocr import isolation
from coinpup_api.ocr.isolation import IsolationError, ProcessBudget, run_isolated

LINUX = pytest.mark.skipif(sys.platform != "linux", reason="Requires Linux resource limits")
ROOT = Path(__file__).resolve().parents[2]
CYCLIC_REQUEST = {}
CYCLIC_REQUEST["self"] = CYCLIC_REQUEST


@pytest.fixture
def budget():
    return ProcessBudget(
        wall_seconds=8,
        cpu_seconds=4,
        address_space_bytes=128 * 1024 * 1024,
        file_bytes=64 * 1024,
        open_files=64,
        output_bytes=16 * 1024,
    )


@pytest.fixture
def processor(tmp_path, monkeypatch):
    package = tmp_path / "coinpup_api"
    entry = package / "ocr"
    entry.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (entry / "__init__.py").write_text("", encoding="utf-8")
    bootstrap = entry / "_isolation_child.py"
    shutil.copyfile(isolation._BOOTSTRAP_PATH, bootstrap)
    shutil.copyfile(ROOT / "tests/fixtures/ocr_process/processor.py", entry / "processor.py")
    monkeypatch.setattr(isolation, "_BOOTSTRAP_PATH", bootstrap)
    return entry


def execute(request, budget):
    return json.loads(run_isolated(request, budget).output)


def failure(code, request, budget):
    with pytest.raises(IsolationError) as caught:
        run_isolated(request, budget)
    assert caught.value.code == code
    return caught.value


def test_budget_is_explicit_and_preserves_all_integer_limits(budget):
    assert budget.wall_seconds == 8 and budget.cpu_seconds == 4
    assert budget.address_space_bytes == 128 * 1024 * 1024
    assert budget.file_bytes == 64 * 1024
    assert budget.open_files == 64 and budget.output_bytes == 16 * 1024


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("wall_seconds", 0),
        ("wall_seconds", True),
        ("cpu_seconds", 1.5),
        ("cpu_seconds", -1),
        ("address_space_bytes", False),
        ("address_space_bytes", 0),
        ("file_bytes", -1),
        ("open_files", 7),
        ("open_files", "64"),
        ("output_bytes", 0),
    ],
)
def test_invalid_budget_never_silently_drops_a_limit(budget, field, value):
    with pytest.raises(ValueError, match="^Invalid isolation budget\\.$"):
        replace(budget, **{field: value})


@pytest.mark.skipif(sys.platform == "linux", reason="Linux supports the real isolation runner")
def test_unsupported_platform_fails_closed_without_running_a_processor(budget):
    error = failure("processor_unavailable", {"mode": "inspect"}, budget)
    assert "inspect" not in str(error)


@LINUX
def test_all_hard_limits_are_installed_before_processor_import(processor, budget):
    request = {"mode": "inspect", "fictional": "虚构测试数据, not document contents"}
    result = execute(request, budget)
    expected = {
        name: [getattr(budget, name)] * 2
        for name in ("cpu_seconds", "address_space_bytes", "file_bytes", "open_files")
    }
    assert result["import_limits"] == result["call_limits"] == expected
    assert result["request"] == request
    # The temporary working directory is gone even after a successful child exit.
    assert not Path(result["working_directory"]).exists()


@LINUX
def test_parent_secrets_and_python_environment_are_not_inherited(processor, budget, monkeypatch):
    fictional = {
        "COINPUP_DATABASE_URL": "postgresql://fictional.invalid/no-real-database",
        "COINPUP_SESSION_SECRET": "fictional-session-marker",
        "AWS_SECRET_ACCESS_KEY": "fictional-cloud-marker",
        "PYTHONPATH": str(processor),
        "PYTHONSTARTUP": str(processor / "must-not-run.py"),
    }
    for key, value in fictional.items():
        monkeypatch.setenv(key, value)
    result = execute({"mode": "inspect"}, budget)
    assert not set(fictional) & result["environment"].keys()
    encoded = json.dumps(result)
    assert all(value not in encoded for value in fictional.values())


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"bad": float("nan")},
        {"bad": object()},
        {"bad": "\ud800"},
        {"nested": {1: "fictional"}},
        CYCLIC_REQUEST,
    ],
)
def test_invalid_request_cannot_reach_the_processor(budget, payload):
    failure("processing_failed", payload, budget)


def test_request_size_is_bounded_before_starting_the_processor(budget):
    failure("processing_failed", {"mode": "inspect", "large": "x" * (1024 * 1024)}, budget)


@LINUX
def test_missing_processor_is_explicitly_unavailable(processor, budget):
    (processor / "processor.py").unlink()
    failure("processor_unavailable", {"mode": "inspect"}, budget)


@LINUX
@pytest.mark.parametrize("payload", ["not-json", "list", "duplicate", "nan", "infinity", "utf8"])
def test_invalid_success_output_is_rejected_instead_of_returned(processor, budget, payload):
    failure("processing_failed", {"mode": "invalid_json", "payload": payload}, budget)


@LINUX
def test_real_address_space_limit_blocks_a_large_allocation(processor, budget):
    result = execute({"mode": "memory", "allocation_bytes": 256 * 1024 * 1024}, budget)
    assert result == {"blocked": True}


@LINUX
def test_real_file_size_limit_blocks_oversized_output(processor, budget):
    result = execute({"mode": "file", "write_bytes": budget.file_bytes * 2}, budget)
    assert result["blocked"] is True and result["size"] == budget.file_bytes


@LINUX
def test_real_descriptor_limit_blocks_unbounded_open_files(processor, budget):
    result = execute({"mode": "descriptors"}, budget)
    assert result["blocked"] is True and 0 < result["opened"] < budget.open_files


@LINUX
def test_real_cpu_limit_terminates_a_busy_processor_before_wall_timeout(processor, budget):
    started = time.monotonic()
    failure("resource_limit", {"mode": "cpu"}, replace(budget, cpu_seconds=1))
    assert time.monotonic() - started < budget.wall_seconds


@LINUX
@pytest.mark.parametrize("descriptors", [[1], [2], [1, 2]])
def test_stdout_stderr_and_combined_output_are_bounded(processor, budget, descriptors):
    # For two streams each is individually below the cap, but their sum exceeds it.
    size = budget.output_bytes + 1 if len(descriptors) == 1 else budget.output_bytes * 3 // 4
    failure(
        "resource_limit",
        {"mode": "output", "descriptors": descriptors, "write_bytes": size},
        budget,
    )


@LINUX
def test_wall_timeout_terminates_a_waiting_processor(processor, budget):
    started = time.monotonic()
    failure("processor_timeout", {"mode": "wait"}, replace(budget, wall_seconds=1))
    assert time.monotonic() - started < budget.wall_seconds


def live_process(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    # An orphan zombie cannot execute or retain descriptors; the host init reaps it.
    return state != "Z"


@LINUX
@pytest.mark.parametrize("parent_waits", [True, False])
def test_descendants_are_killed_on_timeout_and_success(processor, budget, tmp_path, parent_waits):
    marker = tmp_path / "fictional-child.pid"
    request = {"mode": "descendant", "marker": str(marker), "parent_waits": parent_waits}
    try:
        if parent_waits:
            failure("processor_timeout", request, replace(budget, wall_seconds=1))
        else:
            result = execute(request, budget)
            assert result["child"] == int(marker.read_text())
        pid = int(marker.read_text())
        deadline = time.monotonic() + 3
        while live_process(pid) and time.monotonic() < deadline:
            time.sleep(0.01)  # Bounded observation of kernel process state, not race scheduling.
        assert not live_process(pid), "Isolation left a live descendant behind"
    finally:
        # Clean our own fictional child if a regression fails the assertion.
        if marker.exists():
            pid = int(marker.read_text())
            if live_process(pid):
                os.kill(pid, signal.SIGKILL)


@LINUX
def test_errors_never_expose_document_text_paths_or_stderr(processor, budget, capsys, caplog):
    private = "Fictional invoice text at /fictional/private/invoice.pdf"
    error = failure("processing_failed", {"mode": "raise", "private_text": private}, budget)
    assert private not in str(error) and private not in repr(error)
    assert "/fictional/private" not in str(error)
    assert not hasattr(error, "stderr") and not hasattr(error, "output")
    captured = capsys.readouterr()
    assert private not in captured.out + captured.err + caplog.text


@LINUX
def test_success_counts_stderr_without_returning_its_contents(processor, budget):
    result = run_isolated({"mode": "stderr"}, budget)
    assert json.loads(result.output) == {"fictional": True}
    assert result.stderr_bytes == len(b"Fictional diagnostic\n")
    assert "Fictional diagnostic" not in repr(result)
    assert '"fictional"' not in repr(result)
