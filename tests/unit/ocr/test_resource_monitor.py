"""Synthetic kernel files exercise observations, not invented resource enforcement."""

import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from scripts.ocr_benchmark import resource_monitor as monitor


def write_process(proc, pid, *, start=100, rss=10, hwm=15):
    path = proc / str(pid)
    path.mkdir(exist_ok=True)
    fields = ["S"] + ["0"] * 18 + [str(start)] + ["0"] * 5
    (path / "stat").write_text(f"{pid} (fictional ) process) " + " ".join(fields))
    (path / "status").write_text(f"Name:\tfictional\nVmRSS:\t{rss} kB\nVmHWM:\t{hwm} kB")
    (path / "cgroup").write_text("0::/sandbox")
    return path


@pytest.fixture
def kernel(tmp_path, monkeypatch):
    proc, root = tmp_path / "proc", tmp_path / "cgroup"
    (proc / "self").mkdir(parents=True)
    group = root / "sandbox"
    group.mkdir(parents=True)
    (proc / "self/mountinfo").write_text(f"1 0 0:1 / {root} rw - cgroup2 cgroup rw")
    write_process(proc, 101)
    write_process(proc, 202, start=200, rss=20, hwm=25)
    for name, value in {
        "memory.max": "4294967296",
        "memory.swap.max": "0",
        "pids.max": "128",
        "cpu.max": "200000 100000",
        "cpuset.cpus.effective": "2,4",
        "cpu.stat": "usage_usec 100\nuser_usec 70\nsystem_usec 30",
        "memory.events": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0",
        "memory.current": "123456",
        "memory.peak": "234567",
        "cgroup.procs": "101\n202",
    }.items():
        (group / name).write_text(value)
    monkeypatch.setattr(monitor, "PROC_ROOT", proc)
    monkeypatch.setattr(monitor, "CGROUP_ROOT", root)
    monkeypatch.setattr(monitor.sys, "platform", "linux")
    monkeypatch.setattr(monitor.os, "readlink", lambda path: "time:[123]")
    monkeypatch.setattr(monitor.os, "sysconf", lambda name: 100, raising=False)
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=b'{"Running":true,"Pid":101}', stderr=b""
        ),
    )
    return proc, group, tmp_path / "report.json"


def build(kernel):
    return monitor.ResourceMonitor("fictional-container", (2, 4), kernel[2])


def test_real_thread_first_sample_marks_and_idempotent_stop(kernel):
    resource = build(kernel)
    resource.start()
    assert resource.samples  # start must already have a real observation
    initial = resource.mark("ready")
    time.sleep(0.035)
    (kernel[1] / "cpu.stat").write_text("usage_usec 150\nuser_usec 110\nsystem_usec 40")
    ending = resource.mark("measurement_end")
    report = resource.stop()
    assert report["status"] == "complete"
    assert len(report["samples"]) >= 2
    assert report["same_window_rss_peak_bytes"] == 30 * 1024
    assert report["individual_hwm_bytes"] == {"101:100": 15 * 1024, "202:200": 25 * 1024}
    assert report["cpu_usage_delta_usec"] == 50
    assert initial["memory_peak_bytes"] == ending["memory_peak_bytes"] == 234567
    assert all(gap > 0 for gap in report["sample_gaps_ns"])
    assert all(row["sample_start_ns"] <= row["sample_end_ns"] for row in report["samples"])
    assert json.loads(kernel[2].read_text()) == report
    assert resource.stop() is report


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("memory.max", "max"),
        ("memory.swap.max", "1"),
        ("pids.max", "129"),
        ("cpu.max", "100000 100000"),
        ("cpuset.cpus.effective", "2-3"),
    ],
)
def test_actual_limit_mismatch_saves_failure(kernel, name, value):
    (kernel[1] / name).write_text(value)
    resource = build(kernel)
    with pytest.raises(monitor.ResourceMonitorError, match="resource_limits_mismatch"):
        resource.start()
    report = json.loads(kernel[2].read_text())
    assert report["status"] == "partial"
    assert report["same_window_rss_peak_bytes"] is None
    with pytest.raises(monitor.ResourceMonitorError, match=report["reason"]):
        resource.stop()


def test_pid_reuse_has_separate_identity_and_never_sums_hwm(kernel):
    resource = build(kernel)
    resource._resolve()
    resource.baseline = resource._snapshot()
    resource.samples.append(resource._sample())
    write_process(kernel[0], 202, start=201, rss=4, hwm=90)
    resource.samples.append(resource._sample())
    resource._thread = SimpleNamespace(join=lambda timeout: None, is_alive=lambda: False)
    resource._thread_started = True
    report = resource.stop()
    assert report["individual_hwm_bytes"]["202:200"] == 25 * 1024
    assert report["individual_hwm_bytes"]["202:201"] == 90 * 1024
    assert report["same_window_rss_peak_bytes"] == 30 * 1024


def test_disappearance_is_recorded_but_unreadable_live_pid_fails(kernel, monkeypatch):
    resource = build(kernel)
    resource._resolve()
    original = monitor._stat

    def disappearing(path):
        if path == kernel[0] / "202/stat":
            (kernel[1] / "cgroup.procs").write_text("101")
            raise FileNotFoundError
        return original(path)

    monkeypatch.setattr(monitor, "_stat", disappearing)
    sample = resource._sample()
    assert sample["disappeared_pids"] == [202]
    assert sample["same_window_rss_bytes"] == 10 * 1024
    (kernel[1] / "cgroup.procs").write_text("101\n303")
    with pytest.raises(monitor.ResourceMonitorError, match="resource_process_unavailable"):
        resource._sample()


@pytest.mark.parametrize(
    "kind", ["missing_rss", "membership", "reuse_init", "truncated", "cpu_missing"]
)
def test_inconsistent_kernel_facts_fail_closed(kernel, monkeypatch, kind):
    resource = build(kernel)
    resource._resolve()
    if kind == "missing_rss":
        (kernel[0] / "202/status").write_text("VmHWM:\t20 kB")
    elif kind == "membership":
        (kernel[0] / "202/cgroup").write_text("0::/elsewhere")
    elif kind == "reuse_init":
        write_process(kernel[0], 101, start=101)
    elif kind == "truncated":
        monkeypatch.setattr(monitor, "READ_BYTES", 8)
    else:
        (kernel[1] / "cpu.stat").write_text("user_usec 1\nsystem_usec 2")
    with pytest.raises(monitor.ResourceMonitorError):
        resource._sample()


def test_time_namespace_mismatch_and_permission_only_privilege(kernel, monkeypatch):
    calls = []

    def readlink(path):
        if path == kernel[0] / "101/ns/time":
            raise PermissionError
        return "time:[123]"

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"time:[123]\n", stderr=b"")

    resource = build(kernel)
    resource.pid = 101
    monkeypatch.setattr(monitor.os, "readlink", readlink)
    monkeypatch.setattr(monitor.subprocess, "run", run)
    assert resource._namespace() == "time:[123]"
    assert calls[0][0] == ["sudo", "-n", "readlink", str(kernel[0] / "101/ns/time")]
    assert calls[0][1]["timeout"] == 10
    assert resource.namespace_reads[0]["privilege"] == "sudo"
    monkeypatch.setattr(
        monitor.os, "readlink", lambda path: "time:[999]" if "101" in str(path) else "time:[123]"
    )
    with pytest.raises(monitor.ResourceMonitorError, match="resource_time_namespace_mismatch"):
        resource._namespace()
    monkeypatch.setattr(
        monitor.os, "readlink", lambda path: (_ for _ in ()).throw(FileNotFoundError())
    )
    with pytest.raises(FileNotFoundError):
        resource._namespace()
    assert len(calls) == 1


def test_thread_read_failure_latches_and_saves_existing_samples(kernel):
    resource = build(kernel)
    resource.start()
    (kernel[0] / "202/status").write_text("invalid")
    assert resource._halt.wait(1)
    with pytest.raises(monitor.ResourceMonitorError, match="resource_process_unavailable"):
        resource.stop()
    report = json.loads(kernel[2].read_text())
    assert report["samples"] and report["errors"]
    assert report["same_window_rss_peak_bytes"] == 30 * 1024


def test_clock_regression_fails(kernel, monkeypatch):
    resource = build(kernel)
    resource._resolve()
    clock = iter([100, 110, 99])
    monkeypatch.setattr(monitor.time, "monotonic_ns", lambda: next(clock))
    with pytest.raises(monitor.ResourceMonitorError, match="resource_clock_invalid"):
        resource._sample()


def test_identity_changes_during_status_read_fail(kernel, monkeypatch):
    resource = build(kernel)
    resource._resolve()
    original = monitor._read

    def read(path):
        result = original(path)
        if path == kernel[0] / "202/status":
            write_process(kernel[0], 202, start=201)
        return result

    monkeypatch.setattr(monitor, "_read", read)
    with pytest.raises(monitor.ResourceMonitorError, match="resource_process_race"):
        resource._sample()


def test_thread_start_failure_saves_partial(kernel, monkeypatch):
    class Unstarted:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("fictional start failure")

        def join(self, timeout):
            pytest.fail("must not join an unstarted thread")

    monkeypatch.setattr(monitor.threading, "Thread", Unstarted)
    with pytest.raises(monitor.ResourceMonitorError, match="resource_observation_failed"):
        build(kernel).start()
    assert json.loads(kernel[2].read_text())["baseline"] is not None


def test_output_failure_remains_failed_on_second_stop(kernel):
    resource = monitor.ResourceMonitor("fictional", (2, 4), kernel[2].parent / "missing/report")
    resource.start()
    for _ in range(2):
        with pytest.raises(monitor.ResourceMonitorError, match="resource_output_failed"):
            resource.stop()
    assert resource.report["status"] == "partial"


@pytest.mark.parametrize("cpus", [(True, 2), (2, 2), (2,), "0-999999999", "2,4,6", "2;4"])
def test_cpu_configuration_is_bounded(cpus, tmp_path):
    with pytest.raises(monitor.ResourceMonitorError, match="resource_configuration_invalid"):
        monitor.ResourceMonitor("fictional", cpus, tmp_path / "report")


@pytest.mark.parametrize(
    "state", [b'{"Running":false,"Pid":101}', b'{"Running":true,"Pid":true}', b"[]"]
)
def test_bad_inspection_saves_partial(kernel, monkeypatch, state):
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=state, stderr=b""),
    )
    with pytest.raises(monitor.ResourceMonitorError):
        build(kernel).start()
    assert json.loads(kernel[2].read_text())["status"] == "partial"


def test_inspection_timeout_and_escape_cgroup_save_partial(kernel, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("docker", 10)

    monkeypatch.setattr(monitor.subprocess, "run", timeout)
    with pytest.raises(monitor.ResourceMonitorError, match="resource_observation_failed"):
        build(kernel).start()
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=b'{"Running":true,"Pid":101}', stderr=b""
        ),
    )
    (kernel[0] / "101/cgroup").write_text("0::/../sandbox")
    with pytest.raises(monitor.ResourceMonitorError, match="resource_cgroup_invalid"):
        build(kernel).start()
