"""Explicit acquisition and offline inspection of pinned candidate inputs, never inference."""

import argparse
import email.policy
import hashlib
import json
import os
import re
import stat
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from email.parser import BytesParser
from io import BytesIO
from pathlib import Path, PurePosixPath

ASSETS_PATH = Path(__file__).resolve().parents[2] / "tests/fixtures/ocr/engine-assets.json"
_CHUNK, _TIMEOUT, _DEADLINE = 65536, 60, 600
_MAX_FILE, _MAX_TEXT, _MAX_TEXT_TOTAL = 512 * 1024**2, 2 * 1024**2, 8 * 1024**2
_SOURCE_PREFIXES = (
    "https://raw.githubusercontent.com/",
    "https://files.pythonhosted.org/packages/",
    "https://huggingface.co/PaddlePaddle/",
    "https://codeload.github.com/tesseract-ocr/tesseract/",
    "https://paddle-whl.cdn.bcebos.com/stable/cpu/",
    "https://paddlepaddledeps.bj.bcebos.com/",
    "https://changelogs.ubuntu.com/changelogs/pool/",
    "https://archive.ubuntu.com/ubuntu/pool/",
)
_DOWNLOAD_PREFIXES = (
    *_SOURCE_PREFIXES,
    "https://cas-bridge.xethub.hf.co/",
    "https://cdn-lfs.hf.co/",
    "https://cdn-lfs-us-1.hf.co/",
    "https://cdn-lfs-eu-1.hf.co/",
)
_NATIVE = re.compile(r"\.(?:so(?:\.[A-Za-z0-9_.-]+)?|dll|dylib|pyd)$")
_NOTICE = re.compile(r"^(?:license|licence|notice|copying)(?:$|[._-])", re.I)


class EngineAssetError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise EngineAssetError(code) from None


def _name(value):
    if (
        type(value) is not str
        or not value
        or "\\" in value
        or PurePosixPath(value).is_absolute()
        or "//" in value
        or any(
            not re.fullmatch(r"[A-Za-z0-9_.-]+", part)
            or part in (".", "..")
            or part.endswith((".", " "))
            or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", part.split(".")[0], re.I)
            for part in value.split("/")
        )
    ):
        _fail("engine_manifest_invalid")
    return value


def _url(value, *, redirect=False):
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.scheme != "https"
        or not value.startswith(_DOWNLOAD_PREFIXES if redirect else _SOURCE_PREFIXES)
    ):
        _fail("engine_manifest_invalid")
    return value


def _manifest():
    try:
        if ASSETS_PATH.stat().st_size > _MAX_TEXT:
            _fail("engine_manifest_invalid")
        result = json.loads(ASSETS_PATH.read_text(encoding="utf8"))
        names = set()
        if result["version"] != 1 or not 1 <= len(result["files"]) <= 128:
            _fail("engine_manifest_invalid")
        for item in result["files"]:
            name = _name(item["name"])
            _url(item["url"])
            if (
                name in names
                or type(item["byte_size"]) is not int
                or not 0 < item["byte_size"] <= _MAX_FILE
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            ):
                _fail("engine_manifest_invalid")
            names.add(name)
        embedded = result.get("embedded_notices", [])
        if len(embedded) > 32:
            _fail("engine_manifest_invalid")
        for notice in embedded:
            _name(notice["name"])
            _name(notice["archive_member"])
            _url(notice["source_archive"]["url"])
            if notice["text_encoding"] != "utf-8" or len(notice["text"]) > _MAX_TEXT:
                _fail("engine_manifest_invalid")
            data = notice["text"].encode("utf8")
            if (
                len(data) != notice["byte_size"]
                or len(data) > _MAX_TEXT
                or hashlib.sha256(data).hexdigest() != notice["sha256"]
            ):
                _fail("engine_manifest_invalid")
        return result
    except (OSError, ValueError, TypeError, KeyError):
        _fail("engine_manifest_invalid")


def _linked(path):
    return path.is_symlink() or path.is_junction()


def _root(directory, *, create=False):
    root = Path(directory).absolute()
    if any(_linked(part) for part in [root, *root.parents]):
        _fail("engine_asset_invalid")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        _fail("engine_asset_invalid")
    return root


def _path(root, name, *, create=False):
    target = root / _name(name)
    for parent in reversed(target.parents):
        if parent == root or root in parent.parents:
            if _linked(parent):
                _fail("engine_asset_invalid")
            if create:
                parent.mkdir(exist_ok=True)
    if _linked(target):
        _fail("engine_asset_invalid")
    return target


def _digest(stream, size, *, output=None, deadline=None):
    digest, count = hashlib.sha256(), 0
    # HTTP read1 returns after one underlying read, so continuous trickles cannot
    # postpone the deadline check until a full chunk arrives. An in-flight read
    # is still bounded by the socket timeout, not an independent hard wall kill.
    read = getattr(stream, "read1", stream.read) if deadline is not None else stream.read
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            _fail("engine_fetch_failed")
        data = read(min(_CHUNK, size - count + 1))
        if deadline is not None and time.monotonic() >= deadline:
            _fail("engine_fetch_failed")
        if not data:
            break
        count += len(data)
        if count > size:
            _fail("engine_asset_invalid")
        digest.update(data)
        if output is not None:
            output.write(data)
    if count != size:
        _fail("engine_asset_invalid")
    return digest.hexdigest()


def _verify(path, item):
    if not stat.S_ISREG(path.lstat().st_mode):
        _fail("engine_asset_invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != item["byte_size"]:
            _fail("engine_asset_invalid")
        if _digest(stream, item["byte_size"]) != item["sha256"]:
            _fail("engine_asset_invalid")


def verify_assets(output_dir):
    """Read the fixed manifest and verify every listed file without any network operation."""
    try:
        manifest, root = _manifest(), _root(output_dir)
        for item in manifest["files"]:
            _verify(_path(root, item["name"]), item)
        return manifest
    except (OSError, ValueError, TypeError):
        _fail("engine_asset_invalid")


class _Redirect(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        # urllib otherwise drains the redirect body without a size limit.
        fp.close()
        with BytesIO() as empty:
            return super().http_error_302(req, empty, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _url(newurl, redirect=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener():
    return urllib.request.build_opener(_Redirect())


def fetch_assets(output_dir):
    """Explicit network acquisition; atomic publish never replaces an existing cache file."""
    temporary = None
    try:
        manifest, root = _manifest(), _root(output_dir, create=True)
        for item in manifest["files"]:
            target = _path(root, item["name"], create=True)
            if target.exists():
                _verify(target, item)
                continue
            fd, temporary = tempfile.mkstemp(prefix=".engine-", dir=target.parent)
            deadline = time.monotonic() + _DEADLINE
            with os.fdopen(fd, "wb") as output:
                with _opener().open(item["url"], timeout=_TIMEOUT) as response:
                    _url(response.geturl(), redirect=True)
                    digest = _digest(response, item["byte_size"], output=output, deadline=deadline)
                if digest != item["sha256"]:
                    _fail("engine_asset_invalid")
                output.flush()
                os.fsync(output.fileno())
            # A competing publisher can only be accepted after its full identity is checked.
            try:
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError:
                _verify(_path(root, item["name"]), item)
            Path(temporary).unlink()
            temporary = None
        return verify_assets(root)
    except (OSError, ValueError, TypeError):
        _fail("engine_fetch_failed")
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def _member(archive, info, *, text=False):
    with archive.open(info) as stream:
        if text:
            if info.file_size > _MAX_TEXT:
                _fail("engine_wheel_invalid")
            data = stream.read(_MAX_TEXT + 1)
            if len(data) != info.file_size:
                _fail("engine_wheel_invalid")
            return {
                "name": info.filename,
                "byte_size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "text": data.decode("utf8"),
            }
        if info.file_size > _MAX_FILE:
            _fail("engine_wheel_invalid")
        return {
            "name": info.filename,
            "byte_size": info.file_size,
            "sha256": _digest(stream, info.file_size),
        }


def _wheel(path, item, expected):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 10000 or len({i.filename for i in entries}) != len(entries):
            _fail("engine_wheel_invalid")
        for info in entries:
            _name(info.filename.rstrip("/"))
            if stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
                _fail("engine_wheel_invalid")
        metadata = [i for i in entries if i.filename.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            _fail("engine_wheel_invalid")
        base = metadata[0].filename.rsplit("/", 1)[0]
        metadata = _member(archive, metadata[0], text=True)
        wheel = _member(archive, archive.getinfo(base + "/WHEEL"), text=True)
        parser = BytesParser(policy=email.policy.compat32)
        headers, wheel_headers = (
            parser.parsebytes(record["text"].encode("utf8")) for record in (metadata, wheel)
        )
        if not headers["Name"] or not headers["Version"] or not wheel_headers.get_all("Tag"):
            _fail("engine_wheel_invalid")
        declared = headers.get_all("License-File", [])
        notices = {
            i.filename
            for i in entries
            if not i.is_dir() and _NOTICE.match(PurePosixPath(i.filename).name)
        }
        for name in declared:
            _name(name)
            matches = [
                candidate
                for candidate in (base + "/" + name, base + "/licenses/" + name)
                if candidate in archive.namelist()
            ]
            if not matches:
                _fail("engine_wheel_invalid")
            notices.update(matches)
        native = [i for i in entries if _NATIVE.search(i.filename)]
        if (
            sum(archive.getinfo(name).file_size for name in notices) > _MAX_TEXT_TOTAL
            or sum(i.file_size for i in native) > 2 * 1024**3
        ):
            _fail("engine_wheel_invalid")
        native = [_member(archive, info) for info in sorted(native, key=lambda i: i.filename)]
        if expected is not None and native != sorted(expected, key=lambda record: record["name"]):
            _fail("engine_wheel_invalid")
        return {
            "name": item["name"],
            "package": headers["Name"],
            "version": headers["Version"],
            "requires_dist": headers.get_all("Requires-Dist", []),
            "license": headers.get("License"),
            "license_expression": headers.get("License-Expression"),
            "license_files": declared,
            "license_classifiers": [
                c for c in headers.get_all("Classifier", []) if c.startswith("License ::")
            ],
            "tags": wheel_headers.get_all("Tag", []),
            "metadata": metadata,
            "wheel": wheel,
            "notices": [
                _member(archive, archive.getinfo(name), text=True) for name in sorted(notices)
            ],
            "native": native,
        }


def inspect_wheels(output_dir):
    """Inspect complete notices and native bytes, without importing or extracting a wheel."""
    try:
        manifest, root = verify_assets(output_dir), _root(output_dir)
        wheels = [
            _wheel(
                _path(root, item["name"]),
                item,
                manifest.get("native_expected", {}).get(item["name"]),
            )
            for item in manifest["files"]
            if item["role"] == "wheel"
        ]
        return {"version": 1, "wheels": wheels}
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, RuntimeError):
        _fail("engine_wheel_invalid")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "verify", "inspect"))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = {"fetch": fetch_assets, "verify": verify_assets, "inspect": inspect_wheels}[
            args.command
        ](args.output_dir)
    except EngineAssetError as error:
        parser.exit(1, error.code + "\n")
    print(
        json.dumps(
            {
                "version": 1,
                "files": len(result.get("files", [])),
                "bytes": sum(item["byte_size"] for item in result.get("files", [])),
                "wheels": len(result.get("wheels", [])),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
