"""Fictional browser seeds must fail closed before constructing a database engine."""

import importlib
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from coinpup_api.config import Settings
from pydantic import SecretStr


@pytest.mark.parametrize(
    "opt_in,environment,host,name,allowed",
    [
        ("1", "test", "postgres", "coinpup", True),
        ("0", "test", "postgres", "coinpup", False),
        ("1", "production", "postgres", "coinpup", False),
        ("1", "test", "localhost", "coinpup", False),
        ("1", "test", "postgres", "fictional_other", False),
    ],
)
@pytest.mark.parametrize("module", ["create_ocr_browser_fixture", "finish_ocr_browser_fixture"])
def test_guard_unwraps_secret_and_precedes_database(
    monkeypatch, opt_in, environment, host, name, allowed, module
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    checker = importlib.import_module(module)
    monkeypatch.setenv("COINPUP_BROWSER_JOB_ID", "11111111-1111-4111-8111-111111111111")
    monkeypatch.setenv("COINPUP_BROWSER_JOB_ACTION", "finish")
    settings = Settings(_env_file=None).model_copy(
        update={
            "environment": environment,
            "database_url": SecretStr(f"postgresql+psycopg://fictional:fake@{host}/{name}"),
        }
    )
    monkeypatch.setenv("COINPUP_CREATE_OCR_BROWSER_FIXTURE", opt_in)
    monkeypatch.setattr(checker, "Settings", lambda: settings)

    def database(_settings):
        assert allowed, "Opened forbidden engine"
        raise RuntimeError("Reached permitted fixture engine")

    monkeypatch.setattr(checker, "Database", database)
    with pytest.raises(RuntimeError if allowed else SystemExit, match="fixture|Requires"):
        checker.main()


def test_photo_fixture_has_valid_png_chunks_for_strict_browser_decoders(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    data = importlib.import_module("create_ocr_browser_fixture").PHOTO
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    while offset < len(data):
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        body = data[offset + 4 : offset + 8 + size]
        crc = struct.unpack(">I", data[offset + 8 + size : offset + 12 + size])[0]
        assert zlib.crc32(body) & 0xFFFFFFFF == crc
        offset += size + 12
    assert offset == len(data) and body == b"IEND"


@pytest.mark.parametrize(
    "filename,name,allowed",
    [
        ("fictional-browser.pdf", "Fictional E2E OCR fixture", True),
        ("unrelated.pdf", "Fictional E2E OCR fixture", False),
        ("fictional-browser.pdf", "Unrelated entity", False),
        (None, None, False),
    ],
)
def test_worker_outcomes_require_known_fictional_source(monkeypatch, filename, name, allowed):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    checker = importlib.import_module("finish_ocr_browser_fixture")
    row = (SimpleNamespace(created_by="fictional-owner"), filename, name) if filename else None
    session = SimpleNamespace(execute=lambda _query: SimpleNamespace(one_or_none=lambda: row))
    if allowed:
        assert (
            checker.require_fictional_job(session, "11111111-1111-4111-8111-111111111111")
            == "fictional-owner"
        )
    else:
        with pytest.raises(SystemExit, match="known fictional"):
            checker.require_fictional_job(session, "11111111-1111-4111-8111-111111111111")


def test_unreadable_fictional_amount_stays_null_and_rescan_preserves_row_identity(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    build = importlib.import_module("create_ocr_browser_fixture").fictional_completion
    before, after = build(["10.00", "20.00", None]), build(["10.00", "20.00", "30.00"])
    assert [row.source_key for row in before.candidates] == [
        row.source_key for row in after.candidates
    ]
    assert before.candidates[2].recognized["fields"][0]["value"] is None
    assert after.candidates[2].recognized["fields"][0]["value"] == "30.00"
    assert before.candidates[2].fields["review"][0]["requires_confirmation"]
    assert before.candidates[2].fields["review"][0]["suggested_value"] is None
    with pytest.raises(ValueError, match="fixed fictional"):
        build(["123.00"])
