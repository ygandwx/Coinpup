"""Fictional inputs exercise bounded acquisition; no test accesses a remote server."""

import hashlib
import io
import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ocr_benchmark import engine_assets as assets

URL = "https://raw.githubusercontent.com/fictional/pinned/input.bin"


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    monkeypatch.setattr(assets, "ASSETS_PATH", path)

    def write(data=b"fictional input", name="nested/input.bin", *, role="model", **updates):
        item = dict(
            name=name,
            role=role,
            url=URL,
            byte_size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            revision="fictional",
        )
        item.update(updates)
        value = {"version": 1, "files": [item], "native_expected": {}}
        path.write_text(json.dumps(value), encoding="utf8")
        return value

    return write


class Response(io.BytesIO):
    def geturl(self):
        return URL

    def read1(self, size):
        return self.read(size)


def network(monkeypatch, data, *, response_factory=Response):
    calls = []

    def open_response(url, *, timeout):
        calls.append((url, timeout))
        return response_factory(data)

    monkeypatch.setattr(assets, "_opener", lambda: SimpleNamespace(open=open_response))
    return calls


def error(code):
    return pytest.raises(assets.EngineAssetError, match="^" + code + "$")


def test_verified_atomic_publish_and_cache_hit_never_redownload(manifest, tmp_path, monkeypatch):
    payload = b"fictional input"
    expected = manifest(payload)
    calls = network(monkeypatch, payload)
    events, original_link, original_fsync = [], os.link, os.fsync

    def sync(fd):
        events.append("fsync")
        original_fsync(fd)

    def publish(source, target, *, follow_symlinks):
        assert Path(source).read_bytes() == payload
        assert not Path(target).exists() and events == ["fsync"]
        assert follow_symlinks is False
        events.append("publish")
        original_link(source, target, follow_symlinks=False)

    monkeypatch.setattr(assets.os, "fsync", sync)
    monkeypatch.setattr(assets.os, "link", publish)
    output = tmp_path / "cache"
    assert assets.fetch_assets(output) == expected
    assert calls == [(URL, 60)] and events == ["fsync", "publish"]
    assert (output / "nested/input.bin").read_bytes() == payload
    assert assets.fetch_assets(output) == expected
    assert assets.verify_assets(output) == expected
    assert len(calls) == 1 and not list(output.rglob(".engine-*"))


@pytest.mark.parametrize("received", [b"short", b"fictional input!", b"fictional other"])
def test_size_or_hash_failure_leaves_no_published_or_temporary_file(
    manifest, tmp_path, monkeypatch, received
):
    manifest()
    network(monkeypatch, received)
    with error("engine_asset_invalid"):
        assets.fetch_assets(tmp_path / "cache")
    assert not list((tmp_path / "cache").rglob("*.bin"))
    assert not list((tmp_path / "cache").rglob(".engine-*"))


def test_corrupt_existing_cache_is_preserved_without_network(manifest, tmp_path, monkeypatch):
    manifest(name="input.bin")
    target = tmp_path / "input.bin"
    target.write_bytes(b"corrupt bytes")
    calls = network(monkeypatch, b"fictional input")
    with error("engine_asset_invalid"):
        assets.fetch_assets(tmp_path)
    assert target.read_bytes() == b"corrupt bytes" and calls == []


@pytest.mark.parametrize("competing", [b"fictional input", b"corrupt bytes"])
def test_publish_race_accepts_only_fully_verified_competing_file(
    manifest, tmp_path, monkeypatch, competing
):
    manifest(name="input.bin")
    network(monkeypatch, b"fictional input")

    def race(source, target, **kwargs):
        Path(target).write_bytes(competing)
        raise FileExistsError("fictional publication race")

    monkeypatch.setattr(assets.os, "link", race)
    if competing == b"fictional input":
        assets.fetch_assets(tmp_path)
    else:
        with error("engine_asset_invalid"):
            assets.fetch_assets(tmp_path)
    assert (tmp_path / "input.bin").read_bytes() == competing
    assert not list(tmp_path.rglob(".engine-*"))


@pytest.mark.parametrize("stage", ["open", "read", "fsync", "publish"])
def test_transport_or_publication_failure_is_redacted_and_cleans_temporary(
    manifest, tmp_path, monkeypatch, stage
):
    manifest()
    network(monkeypatch, b"fictional input")

    def fail(*args, **kwargs):
        raise OSError("fictional private path / secret transport diagnostics")

    if stage == "open":
        monkeypatch.setattr(assets, "_opener", lambda: SimpleNamespace(open=fail))
    elif stage == "read":
        monkeypatch.setattr(Response, "read", fail)
    else:
        monkeypatch.setattr(assets.os, {"fsync": "fsync", "publish": "link"}[stage], fail)
    with error("engine_fetch_failed") as caught:
        assets.fetch_assets(tmp_path / "cache")
    assert caught.value.code == "engine_fetch_failed"
    assert caught.value.__cause__ is None
    assert not list((tmp_path / "cache").rglob(".engine-*"))
    assert not list((tmp_path / "cache").rglob("*.bin"))


@pytest.mark.parametrize("expire_on_eof", [False, True])
def test_deadline_includes_response_read_and_eof(manifest, tmp_path, monkeypatch, expire_on_eof):
    manifest()
    clock = [0]
    monkeypatch.setattr(assets.time, "monotonic", lambda: clock[0])

    class SlowResponse(Response):
        def read(self, size):
            value = super().read(size)
            if not expire_on_eof or not value:
                clock[0] = 601
            return value

    network(monkeypatch, b"fictional input", response_factory=SlowResponse)
    with error("engine_fetch_failed"):
        assets.fetch_assets(tmp_path / "cache")
    assert not list((tmp_path / "cache").rglob(".engine-*"))


def test_http_read1_trickle_rechecks_deadline_without_waiting_for_a_full_chunk(
    manifest, tmp_path, monkeypatch
):
    manifest()
    clock = [0]
    monkeypatch.setattr(assets.time, "monotonic", lambda: clock[0])

    class TrickleResponse(Response):
        def read(self, size):
            pytest.fail("HTTP read would wait for the full requested chunk")

        def read1(self, size):
            clock[0] += 301
            return b"f"

    network(monkeypatch, b"fictional input", response_factory=TrickleResponse)
    with error("engine_fetch_failed"):
        assets.fetch_assets(tmp_path / "cache")
    assert clock[0] == 602
    assert not list((tmp_path / "cache").rglob(".engine-*"))


@pytest.mark.parametrize(
    "updates",
    [
        {"name": "../escape"},
        {"name": "/absolute"},
        {"name": "a\\b"},
        {"name": "a//b"},
        {"name": "C:/escape"},
        {"name": "a/CON.txt"},
        {"name": "a/LPT1"},
        {"name": "a/end."},
        {"name": "a/../b"},
        {"byte_size": True},
        {"byte_size": 0},
        {"byte_size": 512 * 1024**2 + 1},
        {"sha256": "A" * 64},
        {"url": "http://raw.githubusercontent.com/x"},
        {"url": "https://raw.githubusercontent.com.evil.example/x"},
        {"url": "https://user:secret@raw.githubusercontent.com/x"},
        {"url": URL + "#fragment"},
    ],
)
def test_untrusted_manifest_rejected_before_io(manifest, tmp_path, monkeypatch, updates):
    manifest(**updates)
    calls = network(monkeypatch, b"fictional input")
    with error("engine_manifest_invalid"):
        assets.fetch_assets(tmp_path / "cache")
    assert calls == [] and not (tmp_path / "cache").exists()


def test_duplicate_manifest_paths_are_not_multiple_download_intents(
    manifest, tmp_path, monkeypatch
):
    value = manifest()
    value["files"].append(value["files"][0].copy())
    assets.ASSETS_PATH.write_text(json.dumps(value), encoding="utf8")
    calls = network(monkeypatch, b"fictional input")
    with error("engine_manifest_invalid"):
        assets.fetch_assets(tmp_path / "cache")
    assert calls == []


@pytest.mark.parametrize("defect", [None, "text", "byte_size", "sha256", "text_encoding"])
def test_embedded_notices_are_byte_exact_utf8_before_network(
    manifest, tmp_path, monkeypatch, defect
):
    value = manifest()
    text = "Fictional notice: 原始通知\n"
    data = text.encode("utf8")
    notice = {
        "name": "notice/LICENSE",
        "archive_member": "fictional/LICENSE",
        "text": text,
        "text_encoding": "utf-8",
        "byte_size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "source_archive": {"url": URL, "byte_size": 123, "sha256": "a" * 64},
    }
    if defect:
        notice[defect] = {
            "text": text + "changed",
            "byte_size": len(data) - 1,
            "sha256": "0" * 64,
            "text_encoding": "latin-1",
        }[defect]
    value["embedded_notices"] = [notice]
    assets.ASSETS_PATH.write_text(json.dumps(value), encoding="utf8")
    calls = network(monkeypatch, b"fictional input")
    if defect:
        with error("engine_manifest_invalid"):
            assets.fetch_assets(tmp_path / "cache")
        assert calls == []
    else:
        assert assets.fetch_assets(tmp_path / "cache")["embedded_notices"] == [notice]


@pytest.mark.parametrize("place", ["root", "parent", "target"])
@pytest.mark.parametrize("method", ["is_symlink", "is_junction"])
def test_linked_cache_locations_rejected(manifest, tmp_path, monkeypatch, place, method):
    manifest()
    output = tmp_path / "cache"
    suspicious = {
        "root": output,
        "parent": output / "nested",
        "target": output / "nested/input.bin",
    }[place]
    original = getattr(Path, method)
    monkeypatch.setattr(Path, method, lambda self: self == suspicious or original(self))
    calls = network(monkeypatch, b"fictional input")
    with error("engine_asset_invalid"):
        assets.fetch_assets(output)
    assert calls == []


@pytest.mark.parametrize("url", ["file:///tmp/private", "https://evil.example/payload", URL + "#x"])
def test_redirect_and_final_response_url_validated(manifest, tmp_path, monkeypatch, url):
    manifest()
    req = assets.urllib.request.Request(URL)
    with error("engine_manifest_invalid"):
        assets._Redirect().redirect_request(req, None, 302, "Found", {}, url)

    class Redirected(Response):
        def geturl(self):
            return url

    network(monkeypatch, b"fictional input", response_factory=Redirected)
    with error("engine_manifest_invalid"):
        assets.fetch_assets(tmp_path / "cache")
    assert not list((tmp_path / "cache").rglob(".engine-*"))


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_real_redirect_handler_closes_response_without_draining_unbounded_body(code):
    events, sentinel = [], object()

    def read(*args):
        pytest.fail("Redirect response bodies must never be drained")

    fp = SimpleNamespace(read=read, close=lambda: events.append("closed"))
    req = assets.urllib.request.Request(URL)
    req.timeout = 60
    redirect = "https://cdn-lfs.hf.co/fictional/pinned/input.bin"

    def open_request(new_request, *, timeout):
        assert events == ["closed"]
        assert new_request.full_url == redirect and timeout == 60
        return sentinel

    handler = assets._Redirect()
    handler.parent = SimpleNamespace(open=open_request)
    assert (
        getattr(handler, f"http_error_{code}")(req, fp, code, "Moved", {"location": redirect})
        is sentinel
    )
    assert events == ["closed"]


def fictional_wheel(tmp_path, manifest, *, missing_notice=False, duplicate=False):
    path = tmp_path / "fixture.whl"
    base = "fictional-1.0.dist-info/"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            base + "METADATA",
            "Name: fictional\nVersion: 1.0\n"
            "License: MPL-2.0\nLicense-File: AUTHORS.md\n"
            "Requires-Dist: fictional-dependency>=2\n",
        )
        archive.writestr(base + "WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n")
        if not missing_notice:
            archive.writestr(base + "AUTHORS.md", "Fictional authors, retained in full.\n")
        archive.writestr(
            base + "LICENSE", "MPL-2.0 defines Secondary License and AGPL compatibility.\n"
        )
        archive.writestr("fictional/lib.so", b"fictional native bytes")
        if duplicate:
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr(base + "WHEEL", "Tag: py3-none-any\n")
    value = manifest(path.read_bytes(), name=path.name, role="wheel")
    return value


def test_wheel_inspection_preserves_full_declared_notices_without_license_word_filter(
    manifest, tmp_path
):
    fictional_wheel(tmp_path, manifest)
    wheel = assets.inspect_wheels(tmp_path)["wheels"][0]
    assert (wheel["package"], wheel["version"], wheel["license"]) == ("fictional", "1.0", "MPL-2.0")
    assert wheel["requires_dist"] == ["fictional-dependency>=2"]
    assert wheel["tags"] == ["py3-none-any"]
    assert len(wheel["notices"]) == 2
    for record in wheel["notices"]:
        assert hashlib.sha256(record["text"].encode()).hexdigest() == record["sha256"]
    assert wheel["native"][0]["sha256"] == hashlib.sha256(b"fictional native bytes").hexdigest()


@pytest.mark.parametrize(
    "defect", ["missing_notice", "duplicate", "native_mismatch", "text_budget"]
)
def test_wheel_inspection_rejects_incomplete_or_unbounded_records(
    manifest, tmp_path, monkeypatch, defect
):
    value = fictional_wheel(
        tmp_path, manifest, **{defect: True} if defect in ("missing_notice", "duplicate") else {}
    )
    if defect == "native_mismatch":
        value["native_expected"] = {"fixture.whl": []}
        assets.ASSETS_PATH.write_text(json.dumps(value), encoding="utf8")
    if defect == "text_budget":
        monkeypatch.setattr(assets, "_MAX_TEXT_TOTAL", 1)
    with error("engine_wheel_invalid"):
        assets.inspect_wheels(tmp_path)
