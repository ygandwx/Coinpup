"""Observe an actual Linux cgroup, retaining partial evidence on every failure."""

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

PROC_ROOT = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")
READ_BYTES = 1048576
INTERVAL_NS = 10000000
WAIT_SECONDS = 5


class ResourceMonitorError(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _fail(reason):
    raise ResourceMonitorError(reason)


def _read(path):
    with path.open("rb") as source:
        value = source.read(READ_BYTES + 1)
    if len(value) > READ_BYTES:
        _fail("resource_read_limit")
    return value.decode("ascii", errors="strict").strip()


def _integer(value):
    if not re.fullmatch(r"[0-9]+", value):
        _fail("resource_value_invalid")
    return int(value)


def _cpus(value):
    if isinstance(value, str):
        if len(value) > 100:
            _fail("resource_configuration_invalid")
        result = []
        for item in value.split(","):
            if not re.fullmatch(r"[0-9]+(?:-[0-9]+)?", item):
                _fail("resource_configuration_invalid")
            bounds = [int(part) for part in item.split("-")]
            first, last = bounds[0], bounds[-1]
            if last < first or last - first > 1:
                _fail("resource_configuration_invalid")
            result.extend(range(first, last + 1))
    elif isinstance(value, tuple):
        result = list(value)
    else:
        _fail("resource_configuration_invalid")
    if (
        len(result) != 2
        or any(type(cpu) is not int or cpu < 0 for cpu in result)
        or len(set(result)) != 2
    ):
        _fail("resource_configuration_invalid")
    return tuple(sorted(result))


def _stat(path):
    value = _read(path)
    end = value.rfind(")")
    fields = value[end + 1 :].split()
    if end < 0 or len(fields) < 20 or len(fields[0]) != 1 or fields[0] not in "RSDZTtWXIKP":
        _fail("resource_process_invalid")
    return fields[0], _integer(fields[19])


def _pairs(path):
    result = {}
    for line in _read(path).splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[0] in result:
            _fail("resource_value_invalid")
        result[fields[0]] = _integer(fields[1])
    return result


class ResourceMonitor:
    def __init__(self, container_name, cpus, output_path):
        if not isinstance(container_name, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container_name
        ):
            _fail("resource_configuration_invalid")
        self.name, self.cpus = container_name, _cpus(cpus)
        self.output_path = Path(output_path)
        self.samples, self.marks, self.errors, self.namespace_reads = [], [], [], []
        self.baseline = self.final = self.report = None
        self.pid = self.group = self.group_identity = self.init_starttime = None
        self.namespace = self.membership = None
        self._halt, self._ready, self._lock = (
            threading.Event(),
            threading.Event(),
            threading.Lock(),
        )
        self._thread = None
        self._thread_started = False
        self.clock_ticks = None

    def _error(self, reason):
        with self._lock:
            self.errors.append({"reason": reason, "monotonic_ns": time.monotonic_ns()})
        self._halt.set()
        self._ready.set()

    def _namespace(self):
        path = PROC_ROOT / str(self.pid) / "ns/time"
        try:
            value = os.readlink(path)
        except PermissionError:
            command = ["sudo", "-n", "readlink", str(path)]
            try:
                result = subprocess.run(command, capture_output=True, timeout=10, check=False)
            except (OSError, subprocess.TimeoutExpired):
                self.namespace_reads.append(
                    {"command": command, "privilege": "sudo", "returncode": None}
                )
                _fail("resource_namespace_unavailable")
            self.namespace_reads.append(
                {"command": command, "privilege": "sudo", "returncode": result.returncode}
            )
            if result.returncode or len(result.stdout) > 100 or len(result.stderr) > READ_BYTES:
                _fail("resource_namespace_unavailable")
            value = result.stdout.decode("ascii", errors="strict").strip()
        if not re.fullmatch(r"time:\[[0-9]+\]", value):
            _fail("resource_namespace_invalid")
        host = os.readlink(PROC_ROOT / "self/ns/time")
        if value != host:
            _fail("resource_time_namespace_mismatch")
        return value

    def _resolve(self):
        if sys.platform != "linux":
            _fail("resource_platform_unsupported")
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{json .State}}", self.name],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode or len(result.stdout) > READ_BYTES:
            _fail("resource_container_unavailable")
        state = json.loads(result.stdout)
        if not isinstance(state, dict):
            _fail("resource_container_unavailable")
        self.pid = state.get("Pid")
        if (
            state.get("Running") is not True
            or state.get("Restarting") is True
            or type(self.pid) is not int
            or self.pid <= 0
        ):
            _fail("resource_container_unavailable")
        self.membership = _read(PROC_ROOT / str(self.pid) / "cgroup")
        if not self.membership.startswith("0::/") or "\n" in self.membership:
            _fail("resource_cgroup_invalid")
        relative = Path(self.membership[3:])
        if ".." in relative.parts:
            _fail("resource_cgroup_invalid")
        mount_root = None
        for line in _read(PROC_ROOT / "self/mountinfo").splitlines():
            before, separator, after = line.partition(" - ")
            fields = before.split()
            if separator and len(fields) >= 5 and after.split() and after.split()[0] == "cgroup2":
                if fields[4] == str(CGROUP_ROOT):
                    mount_root = Path(fields[3])
        if mount_root is None:
            _fail("resource_cgroup_invalid")
        try:
            suffix = relative.relative_to(mount_root)
        except ValueError:
            _fail("resource_cgroup_invalid")
        root = CGROUP_ROOT.resolve(strict=True)
        self.group = (root / suffix).resolve(strict=True)
        if not self.group.is_relative_to(root) or self.group == root:
            _fail("resource_cgroup_invalid")
        stat = self.group.stat()
        self.group_identity = stat.st_dev, stat.st_ino
        _, self.init_starttime = _stat(PROC_ROOT / str(self.pid) / "stat")
        self.namespace = self._namespace()
        self.clock_ticks = os.sysconf("SC_CLK_TCK")
        if type(self.clock_ticks) is not int or self.clock_ticks <= 0:
            _fail("resource_clock_invalid")

    def _identity(self):
        stat = self.group.stat()
        if (stat.st_dev, stat.st_ino) != self.group_identity:
            _fail("resource_identity_changed")
        if _read(PROC_ROOT / str(self.pid) / "cgroup") != self.membership:
            _fail("resource_identity_changed")
        _, starttime = _stat(PROC_ROOT / str(self.pid) / "stat")
        if starttime != self.init_starttime:
            _fail("resource_identity_changed")

    def _snapshot(self):
        self._identity()
        limits = {
            key: _read(self.group / key)
            for key in (
                "memory.max",
                "memory.swap.max",
                "pids.max",
                "cpu.max",
                "cpuset.cpus.effective",
            )
        }
        cpu = limits["cpu.max"].split()
        if (
            limits["memory.max"] != "4294967296"
            or limits["memory.swap.max"] != "0"
            or limits["pids.max"] != "128"
            or len(cpu) != 2
            or _integer(cpu[1]) == 0
            or _integer(cpu[0]) != 2 * _integer(cpu[1])
            or _cpus(limits["cpuset.cpus.effective"]) != self.cpus
        ):
            _fail("resource_limits_mismatch")
        cpu_stat, events = _pairs(self.group / "cpu.stat"), _pairs(self.group / "memory.events")
        if (
            not {"usage_usec", "user_usec", "system_usec"} <= cpu_stat.keys()
            or not {"oom", "oom_kill"} <= events.keys()
        ):
            _fail("resource_value_invalid")
        current = _integer(_read(self.group / "memory.current"))
        peak = _integer(_read(self.group / "memory.peak"))
        if peak < current:
            _fail("resource_value_invalid")
        return {
            "monotonic_ns": time.monotonic_ns(),
            "limits": limits,
            "cpu_stat": cpu_stat,
            "memory_events": events,
            "memory_current_bytes": current,
            "memory_peak_bytes": peak,
        }

    def _pids(self):
        pids = [_integer(value) for value in _read(self.group / "cgroup.procs").split()]
        if len(pids) > 128 or len(pids) != len(set(pids)) or any(pid <= 0 for pid in pids):
            _fail("resource_process_invalid")
        if self.pid not in pids:
            _fail("resource_identity_changed")
        return pids

    def _sample(self):
        started = time.monotonic_ns()
        snapshot, processes, disappeared = self._snapshot(), [], []
        for pid in self._pids():
            path = PROC_ROOT / str(pid)
            try:
                state, starttime = _stat(path / "stat")
                if _read(path / "cgroup") != self.membership:
                    _fail("resource_identity_changed")
                status = _read(path / "status")
                after_state, after_start = _stat(path / "stat")
                if _read(path / "cgroup") != self.membership:
                    _fail("resource_identity_changed")
            except FileNotFoundError:
                if pid != self.pid and pid not in self._pids():
                    disappeared.append(pid)
                    continue
                _fail("resource_process_unavailable")
            if after_start != starttime:
                _fail("resource_process_race")
            values = {}
            for key in ("VmRSS", "VmHWM"):
                found = re.findall(rf"^{key}:\s+([0-9]+) kB\r?$", status, flags=re.MULTILINE)
                if len(found) != 1:
                    if state in "ZX" and after_state in "ZX" and pid != self.pid:
                        disappeared.append(pid)
                        break
                    _fail("resource_process_unavailable")
                values[key] = int(found[0]) * 1024
            if len(values) == 2:
                if values["VmHWM"] < values["VmRSS"]:
                    _fail("resource_process_invalid")
                processes.append(
                    {
                        "pid": pid,
                        "starttime": starttime,
                        "rss_bytes": values["VmRSS"],
                        "hwm_bytes": values["VmHWM"],
                    }
                )
        ended = time.monotonic_ns()
        if ended < started:
            _fail("resource_clock_invalid")
        return {
            **snapshot,
            "sample_start_ns": started,
            "sample_end_ns": ended,
            "same_window_rss_bytes": sum(row["rss_bytes"] for row in processes),
            "processes": processes,
            "disappeared_pids": disappeared,
        }

    def _run(self):
        due = time.monotonic_ns()
        try:
            while not self._halt.is_set():
                sample = self._sample()
                with self._lock:
                    if (
                        self.samples
                        and sample["sample_start_ns"] < self.samples[-1]["sample_end_ns"]
                    ):
                        _fail("resource_clock_invalid")
                    if len(self.samples) >= 1000000:
                        _fail("resource_sample_limit")
                    self.samples.append(sample)
                self._ready.set()
                due = max(due + INTERVAL_NS, time.monotonic_ns())
                self._halt.wait(max(0, due - time.monotonic_ns()) / 1000000000)
        except ResourceMonitorError as error:
            self._error(error.reason)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            self._error("resource_observation_failed")

    def start(self):
        if self._thread is not None or self.report is not None:
            _fail("resource_state_invalid")
        try:
            self._resolve()
            self.baseline = self._snapshot()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            self._thread_started = True
            if not self._ready.wait(WAIT_SECONDS):
                _fail("resource_start_timeout")
            if self.errors:
                _fail(self.errors[0]["reason"])
        except ResourceMonitorError as error:
            if not self.errors:
                self._error(error.reason)
            self.stop()
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
            self._error("resource_observation_failed")
            self.stop()

    def mark(self, phase):
        if self._thread is None or self.report is not None:
            _fail("resource_state_invalid")
        if not isinstance(phase, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", phase):
            _fail("resource_configuration_invalid")
        try:
            if self.errors:
                _fail(self.errors[0]["reason"])
            result = {"phase": phase, **self._snapshot()}
            self.marks.append(result)
            return result
        except ResourceMonitorError as error:
            if not self.errors:
                self._error(error.reason)
            self.stop()
        except (OSError, ValueError, KeyError, TypeError):
            self._error("resource_observation_failed")
            self.stop()

    def stop(self):
        if self.report is None:
            if self._thread is None and not self.errors:
                self._error("resource_state_invalid")
            self._halt.set()
            if self._thread is not None and self._thread_started:
                self._thread.join(WAIT_SECONDS)
                if self._thread.is_alive():
                    self._error("resource_stop_timeout")
            if self.group is not None:
                try:
                    self.final = self._snapshot()
                    if self.baseline and (
                        self.final["monotonic_ns"] < self.baseline["monotonic_ns"]
                        or self.final["cpu_stat"]["usage_usec"]
                        < self.baseline["cpu_stat"]["usage_usec"]
                        or self.final["memory_peak_bytes"] < self.baseline["memory_peak_bytes"]
                    ):
                        _fail("resource_clock_invalid")
                except ResourceMonitorError as error:
                    self._error(error.reason)
                except (OSError, ValueError, KeyError, TypeError):
                    self._error("resource_observation_failed")
            with self._lock:
                samples, errors = list(self.samples), list(self.errors)
            hwm = {}
            for sample in samples:
                for row in sample["processes"]:
                    identity = f"{row['pid']}:{row['starttime']}"
                    hwm[identity] = max(hwm.get(identity, 0), row["hwm_bytes"])
            self.report = {
                "version": 1,
                "status": "partial" if errors else "complete",
                "reason": errors[0]["reason"] if errors else None,
                "container": self.name,
                "cpus": list(self.cpus),
                "init_pid": self.pid,
                "init_starttime_ticks": self.init_starttime,
                "clock_ticks_per_second": self.clock_ticks,
                "cgroup_identity": list(self.group_identity) if self.group_identity else None,
                "cgroup": str(self.group) if self.group else None,
                "time_namespace": self.namespace,
                "namespace_reads": self.namespace_reads,
                "interval_ns": INTERVAL_NS,
                "baseline": self.baseline,
                "final": self.final,
                "marks": self.marks,
                "samples": samples,
                "errors": errors,
                "same_window_rss_peak_bytes": max(
                    (row["same_window_rss_bytes"] for row in samples), default=None
                ),
                "individual_hwm_bytes": hwm,
                "sample_gaps_ns": [
                    b["sample_start_ns"] - a["sample_start_ns"]
                    for a, b in zip(samples, samples[1:], strict=False)
                ],
                "cpu_usage_delta_usec": self.final["cpu_stat"]["usage_usec"]
                - self.baseline["cpu_stat"]["usage_usec"]
                if self.final and self.baseline
                else None,
            }
            temporary = self.output_path.with_name(self.output_path.name + ".tmp")
            try:
                temporary.write_text(json.dumps(self.report, allow_nan=False), encoding="utf-8")
                temporary.chmod(0o644)
                os.replace(temporary, self.output_path)
            except OSError:
                self.report["status"] = "partial"
                self.report["reason"] = "resource_output_failed"
                _fail("resource_output_failed")
        if self.report["reason"]:
            _fail(self.report["reason"])
        return self.report
