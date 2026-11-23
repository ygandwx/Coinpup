"""Real bounded child, complete output framing and cleanup using only fictional inputs."""

import hashlib
import signal
import subprocess
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from coinpup_api.business import invoice_render_process as process
from coinpup_api.business.invoice_pdf import render_invoice_text_pdf
from coinpup_api.ledger.errors import MoneyError

from tests.invoice.test_pdf_render_core import fonts as fonts
from tests.invoice.test_pdf_render_core import inspect
from tests.unit.business.test_invoice_render_snapshot import freeze, inputs

pytestmark = pytest.mark.invoice


def run(frozen, fonts, **kwargs):
    return process.render_invoice_pair(
        frozen.content, expected_sha256=frozen.sha256, font_directory=fonts, **kwargs
    )


@pytest.fixture
def children(monkeypatch, tmp_path):
    observed = []
    original = subprocess.Popen

    def start(*args, **kwargs):
        child = original(*args, **kwargs)
        observed.append((child, Path(kwargs["cwd"])))
        return child

    monkeypatch.setattr(process.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(process.subprocess, "Popen", start)
    yield observed
    for child, directory in observed:
        assert child.poll() is not None
        assert not directory.exists()
        assert child.stdout.closed and child.stderr.closed


@pytest.fixture
def bootstrap(monkeypatch, tmp_path):
    def set_code(code):
        path = tmp_path / "fictional_child.py"
        path.write_text(code)
        monkeypatch.setattr(process, "_BOOTSTRAP_PATH", path)

    return set_code


@pytest.mark.parametrize("count,long_lines", [(1, False), (200, False), (200, True)])
def test_actual_child_returns_both_immutable_exact_language_bytes(
    fonts, children, count, long_lines
):
    args = inputs(count)
    if long_lines:
        for index, row in enumerate(args[0].document.lines):
            row.description = f"ROW-{index:03d} " + "虚构測試" * 498
            assert len(row.description) == 2000
    frozen = freeze(*args)
    pair = run(frozen, fonts)
    assert [item.language for item in pair] == ["zh", "en"]
    assert len(children) == 1 and children[0][0].returncode == 0
    for item in pair:
        text, pages = inspect(item.content)
        assert "Fictional" in text
        if long_lines:
            assert pages > 100
            assert all(text.count(f"ROW-{index:03d}") == 1 for index in range(200))
        assert item.content == render_invoice_text_pdf(
            frozen.content,
            expected_sha256=frozen.sha256,
            language=item.language,
            font_directory=fonts,
        )
        assert item.sha256 == hashlib.sha256(item.content).hexdigest()
        assert "Fictional" not in repr(item)
        with pytest.raises(FrozenInstanceError):
            item.content = b"other"


def test_real_child_suppresses_logo_failure_and_never_returns_only_one_pdf(fonts, children):
    with pytest.raises(MoneyError) as caught:
        run(freeze(*inputs(with_logo=True)), fonts)
    assert caught.value.code == "invoice_render_logo_unavailable"
    assert children[0][0].returncode == 76


@pytest.mark.parametrize(
    "field,value",
    [
        ("wall_seconds", 46),
        ("cpu_seconds", 31),
        ("address_space_bytes", 2**30 + 1),
        ("open_files", 65),
        ("output_bytes", True),
        ("file_bytes", 0),
    ],
)
def test_resource_budgets_cannot_be_increased_or_coerced(field, value):
    with pytest.raises(ValueError):
        replace(process.DEFAULT_BUDGET, **{field: value})


def test_bad_owned_digest_refuses_to_start_a_child(fonts, children):
    with pytest.raises(MoneyError) as caught:
        process.render_invoice_pair(b"{}", expected_sha256="f" * 64, font_directory=fonts)
    assert caught.value.code == "invoice_render_integrity" and children == []


def test_no_implicit_unbounded_non_linux_fallback(fonts, children, monkeypatch):
    monkeypatch.setattr(process.sys, "platform", "win32")
    with pytest.raises(MoneyError) as caught:
        run(freeze(*inputs()), fonts)
    assert caught.value.code == "invoice_render_unavailable" and children == []


def test_child_environment_and_input_permissions_do_not_inherit_secrets(
    fonts, bootstrap, children, monkeypatch
):
    monkeypatch.setenv("COINPUP_FICTIONAL_PRIVATE_SENTINEL", "never-forward-this")
    monkeypatch.setenv("PYTHONSTARTUP", "/fictional/startup.py")
    bootstrap("""import os,sys,stat
from pathlib import Path
assert 'COINPUP_FICTIONAL_PRIVATE_SENTINEL' not in os.environ
assert 'PYTHONSTARTUP' not in os.environ and 'PYTHONPATH' not in os.environ
assert 'HOME' not in os.environ and 'LD_PRELOAD' not in os.environ
assert sys.flags.isolated and sys.flags.no_site
assert stat.S_IMODE(Path.cwd().stat().st_mode) == 0o700
assert stat.S_IMODE(Path('snapshot.json').stat().st_mode) == 0o600
assert os.getsid(0) == os.getpid()
raise SystemExit(76)
""")
    with pytest.raises(MoneyError) as caught:
        run(freeze(*inputs()), fonts)
    assert caught.value.code == "invoice_render_logo_unavailable"
    assert children[0][0].returncode == 76


def test_wall_timeout_terminates_and_reaps_real_child(fonts, bootstrap, children):
    bootstrap("import time; time.sleep(60)")
    with pytest.raises(MoneyError) as caught:
        run(freeze(*inputs()), fonts, budget=replace(process.DEFAULT_BUDGET, wall_seconds=1))
    assert caught.value.code == "invoice_render_timeout"
    assert children[0][0].returncode == -signal.SIGTERM


def test_combined_stderr_bound_kills_child_without_exposing_output(fonts, bootstrap, children):
    bootstrap("import os; os.write(2,b'fictional-private-source'*10000)")
    with pytest.raises(MoneyError) as caught:
        run(freeze(*inputs()), fonts, budget=replace(process.DEFAULT_BUDGET, output_bytes=100))
    assert caught.value.code == "invoice_render_limit"
    assert "fictional-private-source" not in str(caught.value)


@pytest.mark.parametrize("failure", ["prefix", "one-language", "extra", "digest", "ownership"])
def test_partial_or_corrupt_pair_is_not_a_completed_render(fonts, bootstrap, children, failure):
    # A fake child only for wire corruption. Successful rendering above uses the real child.
    content = b"%PDF-1.4\nfictional\n%%EOF\n"
    frozen = freeze(*inputs())
    header = process.MAGIC + bytes.fromhex(frozen.sha256)
    header += len(content).to_bytes(4, "big") * 2 + hashlib.sha256(content).digest() * 2
    output = header + content * 2
    if failure == "prefix":
        output = b"wrong" + output
    elif failure == "one-language":
        output = header + content
    elif failure == "extra":
        output += b"extra"
    elif failure == "ownership":
        output = process.MAGIC + b"x" * 32 + output[len(process.MAGIC) + 32 :]
    else:
        output = output[:-8] + b"corrupt!"
    bootstrap("import sys; sys.stdout.buffer.write(" + repr(output) + ")")
    with pytest.raises(MoneyError):
        run(frozen, fonts)
    assert children[0][0].returncode == 0


def test_real_bootstrap_memory_limit_precedes_application_imports(fonts, children):
    budget = replace(process.DEFAULT_BUDGET, address_space_bytes=16 * 1024 * 1024)
    # The 200-row input requires actual font parsing and rendering in the child.
    with pytest.raises(MoneyError) as caught:
        run(freeze(*inputs(200)), fonts, budget=budget)
    assert caught.value.code == "invoice_render_limit"
    assert children[0][0].returncode in (72, -signal.SIGKILL, -signal.SIGXCPU)


def test_real_cpu_limit_stops_work_independent_of_render_speed(fonts, bootstrap, children):
    actual = process._BOOTSTRAP_PATH
    bootstrap(f"""import runpy
runpy.run_path({str(actual)!r})['_limits']([str(1024**3),'1',str(8*1024*1024),'64'])
while True: pass
""")
    with pytest.raises(MoneyError) as caught:
        run(freeze(*inputs()), fonts)
    assert caught.value.code == "invoice_render_limit"
    assert children[0][0].returncode in (-signal.SIGKILL, -signal.SIGXCPU)


def test_timeout_escalates_for_a_child_ignoring_term(fonts, bootstrap, children):
    bootstrap("import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)")
    with pytest.raises(MoneyError) as caught:
        run(freeze(*inputs()), fonts, budget=replace(process.DEFAULT_BUDGET, wall_seconds=1))
    assert caught.value.code == "invoice_render_timeout"
    assert children[0][0].returncode == -signal.SIGKILL


def test_exited_leader_cannot_leave_a_descendant_holding_output_open(
    fonts, bootstrap, children, tmp_path
):
    import time

    record = tmp_path / "descendant.pid"
    bootstrap(f"""import os,signal,time
from pathlib import Path
pid=os.fork()
if pid == 0:
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    Path({str(record)!r}).write_text(str(os.getpid()))
    time.sleep(60)
else:
    while not Path({str(record)!r}).exists(): time.sleep(.001)
    raise SystemExit(76)
""")
    with pytest.raises(MoneyError) as caught:
        run(freeze(*inputs()), fonts)
    assert caught.value.code == "invoice_render_logo_unavailable"
    descendant = Path("/proc") / record.read_text() / "stat"
    deadline = time.monotonic() + 1
    while True:
        try:
            state = descendant.read_text().split(") ", 1)[1][0]
        except (FileNotFoundError, ProcessLookupError):
            break  # The kernel may reap it between lookup and reading /proc.
        if state == "Z":
            break
        assert time.monotonic() < deadline, "descendant remained live"
        time.sleep(0.01)


def test_fixed_bootstrap_installs_inherited_lower_limits_without_changing_parent(
    fonts, bootstrap, children
):
    import resource

    original = resource.getrlimit(resource.RLIMIT_NOFILE)
    actual = process._BOOTSTRAP_PATH
    bootstrap(f"""import runpy,sys,resource
limits=runpy.run_path({str(actual)!r})['_limits']
resource.setrlimit(resource.RLIMIT_NOFILE,(32,64))
limits([str(1024**3),'30',str(8*1024*1024),'64'])
assert resource.getrlimit(resource.RLIMIT_NOFILE)==(32,32)
assert resource.getrlimit(resource.RLIMIT_CORE)==(0,0)
raise SystemExit(76)
""")
    with pytest.raises(MoneyError) as caught:
        run(freeze(*inputs()), fonts)
    assert caught.value.code == "invoice_render_logo_unavailable"
    assert resource.getrlimit(resource.RLIMIT_NOFILE) == original
