"""Actual subprocess gate/descriptor behavior, with no SDK or model initialization."""

import io
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ocr_benchmark import candidate_bootstrap as bootstrap


def frames(writer):
    return [json.loads(line) for line in writer.getvalue().splitlines()]


def test_boot_precedes_any_candidate_import_and_preserves_remaining_binary_requests(monkeypatch):
    calls, writer = [], io.BytesIO()
    reader = io.BytesIO(b'{"v":1,"action":"go"}\n{"v":1,"id":1,"action":"finish"}\n')

    def import_candidate(name):
        assert frames(writer) == [{"v": 1, "event": "boot", "pid": bootstrap.os.getpid()}]
        assert reader.tell() == len(b'{"v":1,"action":"go"}\n')
        calls.append(name)

        def main(*, protocol_writer):
            assert protocol_writer is writer and not writer.closed
            assert reader.read() == b'{"v":1,"id":1,"action":"finish"}\n'
            return 7

        return SimpleNamespace(main=main)

    monkeypatch.setattr(bootstrap.importlib, "import_module", import_candidate)
    assert bootstrap.run_gate(reader, writer) == 7
    assert calls == ["scripts.ocr_benchmark.candidate_session"] and not writer.closed


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b'{"v":1,"action":"go"}',
        b'{"v":1,"v":1,"action":"go"}\n',
        b'{"v":true,"action":"go"}\n',
        b'{"v":1.0,"action":"go"}\n',
        b'{"v":2,"action":"go"}\n',
        b'{"v":1,"action":"go","config":{}}\n',
        b'{"v":1,"action":"start"}\n',
        b'{"v":NaN,"action":"go"}\n',
        b'{"v":Infinity,"action":"go"}\n',
        b'{"v":1,"action":"go"}{}\n',
        b"[]\n",
        b"\xff\n",
        b" " * 1024 + b"\n",
    ],
    ids=[
        "eof",
        "partial",
        "duplicate",
        "bool",
        "float",
        "version",
        "extra",
        "action",
        "nan",
        "infinity",
        "trailing",
        "array",
        "utf8",
        "large",
    ],
)
def test_invalid_gate_never_imports_or_checks_models(monkeypatch, raw):
    calls, writer = [], io.BytesIO()
    monkeypatch.setattr(bootstrap.importlib, "import_module", lambda name: calls.append(name))
    assert bootstrap.run_gate(io.BytesIO(raw), writer) == 1
    assert calls == [] and frames(writer)[-1] == {
        "v": 1,
        "event": "startup_failed",
        "reason": "candidate_boot_invalid",
    }


def test_gate_read_is_bounded_including_its_newline(monkeypatch):
    go = b'{"v":1,"action":"go"}'
    sizes = []

    class Reader(io.BytesIO):
        def readline(self, size=-1):
            sizes.append(size)
            return super().readline(size)

    monkeypatch.setattr(
        bootstrap.importlib, "import_module", lambda name: SimpleNamespace(main=lambda **kwargs: 0)
    )
    exact = go + b" " * (1024 - len(go) - 1) + b"\n"
    assert bootstrap.run_gate(Reader(exact), io.BytesIO()) == 0
    assert bootstrap.run_gate(Reader(exact[:-1] + b" \n"), io.BytesIO()) == 1
    assert sizes == [1025, 1025]


@pytest.mark.parametrize("stage", ["import", "main", "code"])
def test_candidate_failure_does_not_echo_exception_or_paths(monkeypatch, stage):
    def import_candidate(name):
        if stage == "import":
            raise ImportError("FICTIONAL SECRET /private/model")

        def main(**kwargs):
            if stage == "main":
                raise RuntimeError("FICTIONAL SECRET /private/model")
            return True

        return SimpleNamespace(main=main)

    monkeypatch.setattr(bootstrap.importlib, "import_module", import_candidate)
    writer = io.BytesIO()
    assert bootstrap.run_gate(io.BytesIO(b'{"v":1,"action":"go"}\n'), writer) == 1
    assert frames(writer)[-1]["reason"] == "candidate_boot_failed"
    assert b"FICTIONAL SECRET" not in writer.getvalue() and b"/private" not in writer.getvalue()


def test_broken_writer_is_a_failure_without_candidate_import(monkeypatch):
    calls, writer = [], io.BytesIO()
    writer.close()
    monkeypatch.setattr(bootstrap.importlib, "import_module", lambda name: calls.append(name))
    assert bootstrap.run_gate(io.BytesIO(b'{"v":1,"action":"go"}\n'), writer) == 1
    assert calls == []


def test_main_owns_exactly_one_protocol_descriptor_until_candidate_returns(monkeypatch, tmp_path):
    handles, events = [], []
    original_dup = os.dup

    def dup(fd):
        events.append("dup")
        return original_dup(fd)

    def gate(reader, writer):
        assert not writer.closed
        handles.append(writer)
        writer.write(b"FICTIONAL PROTOCOL")
        return 7

    with (tmp_path / "fictional-protocol").open("w+b") as original:
        monkeypatch.setattr(
            bootstrap,
            "sys",
            SimpleNamespace(
                stdout=original, stderr=original, stdin=SimpleNamespace(buffer=io.BytesIO())
            ),
        )
        monkeypatch.setattr(
            bootstrap,
            "os",
            SimpleNamespace(
                dup=dup, dup2=lambda a, b: events.append("dup2"), fdopen=os.fdopen, close=os.close
            ),
        )
        monkeypatch.setattr(bootstrap, "run_gate", gate)
        assert bootstrap.main() == 7 and events == ["dup", "dup2"]
        assert handles[0].closed and not original.closed
        original.seek(0)
        assert original.read() == b"FICTIONAL PROTOCOL"


def test_actual_waiting_process_imports_nothing_until_go_and_redirects_native_stdout_once(tmp_path):
    source = Path(__file__).resolve().parents[3] / "services/api/src"
    marker = tmp_path / "fictional-import-marker"
    code = f"""
import sys, os
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, {str(source)!r})
from scripts.ocr_benchmark import candidate_bootstrap as bootstrap
assert 'scripts.ocr_benchmark.candidate_session' not in sys.modules
assert not {{'PIL','pdfplumber','pypdfium2','paddle','paddleocr','cv2'}} & set(sys.modules)
original_dup, original_dup2 = os.dup, os.dup2
counts = {{'dup':0,'dup2':0}}
def dup(fd):
    counts['dup'] += 1
    return original_dup(fd)
def dup2(a, b):
    counts['dup2'] += 1
    return original_dup2(a, b)
bootstrap.os.dup, bootstrap.os.dup2 = dup, dup2
def imported(name):
    Path({str(marker)!r}).write_text(str(os.getpid()))
    def main(*, protocol_writer):
        assert counts == {{'dup':1,'dup2':1}}
        os.write(1, b'FICTIONAL_NATIVE_NOISE\\n')
        rest = sys.stdin.buffer.read()
        protocol_writer.write(b'{{"v":1,"event":"ready"}}\\n')
        protocol_writer.flush()
        assert rest == b'{{"v":1,"id":1,"action":"finish"}}\\n'
        return 0
    return SimpleNamespace(main=main)
bootstrap.importlib.import_module = imported
raise SystemExit(bootstrap.main())
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    boot_line = queue.Queue()
    reader = threading.Thread(target=lambda: boot_line.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        # The boot line is emitted before readline blocks; no sleeps or SDK mocks claim timings.
        boot = json.loads(boot_line.get(timeout=15))
        reader.join(timeout=5)
        assert not reader.is_alive()
        assert boot.keys() == {"v", "event", "pid"}
        assert boot["v"] == 1 and boot["event"] == "boot"
        assert type(boot["pid"]) is int and boot["pid"] > 0
        assert process.poll() is None and not marker.exists()
        stdout, stderr = process.communicate(
            b'{"v":1,"action":"go"}\n{"v":1,"id":1,"action":"finish"}\n', timeout=15
        )
        # Windows' venv launcher PID can differ from the actual Python worker PID.
        assert process.returncode == 0 and int(marker.read_text()) == boot["pid"]
        assert stdout == b'{"v":1,"event":"ready"}\n' and b"FICTIONAL_NATIVE_NOISE" in stderr
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=15)
        reader.join(timeout=5)
