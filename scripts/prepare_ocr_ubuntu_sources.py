"""Recover two retired Ubuntu descriptors before running the frozen OCR fetch/verify commands."""

import argparse
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from scripts.ocr_benchmark import engine_assets as assets

_SNAPSHOTS = {
    f"https://archive.ubuntu.com/ubuntu/pool/main/{path}": f"https://snapshot.ubuntu.com/ubuntu/20261004T000000Z/pool/main/{path}"
    for path in (
        "libp/libpng1.6/libpng1.6_1.6.43-5ubuntu0.6.dsc",
        "z/zlib/zlib_1.3.dfsg-3.1ubuntu2.2.dsc",
    )
}


class _NoSnapshotRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def prepare(output_dir: Path) -> None:
    root = assets._root(output_dir, create=True)
    for item in assets._manifest()["files"]:
        original = item["url"]
        snapshot = _SNAPSHOTS.get(original)
        if snapshot is None:
            continue
        target = assets._path(root, item["name"], create=True)
        if target.exists():
            # The frozen fetcher verifies every cache hit before use.
            continue
        try:
            with assets._opener().open(original, timeout=assets._TIMEOUT):
                continue  # Original is available; the frozen fetcher will acquire it.
        except urllib.error.HTTPError as error:
            error.close()
            if error.code != 404 or error.geturl() != original:
                raise

        opener = urllib.request.build_opener(_NoSnapshotRedirect())
        deadline = time.monotonic() + assets._DEADLINE
        # Match the frozen fetcher's private staging mode and no-replace publication.
        with tempfile.NamedTemporaryFile(dir=target.parent, delete_on_close=False) as file:
            with opener.open(snapshot, timeout=assets._TIMEOUT) as response:
                if response.geturl() != snapshot:
                    raise assets.EngineAssetError("engine_manifest_invalid")
                digest = assets._digest(response, item["byte_size"], output=file, deadline=deadline)
                if digest != item["sha256"]:
                    raise assets.EngineAssetError("engine_asset_invalid")
            file.flush()
            os.fsync(file.fileno())
            file.close()
            try:
                os.link(file.name, target, follow_symlinks=False)
            except FileExistsError:
                assets._verify(target, item)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    try:
        prepare(parser.parse_args().output_dir)
    except assets.EngineAssetError as exc:
        parser.exit(1, exc.code + "\n")
    except (OSError, ValueError):
        parser.exit(1, "engine_fetch_failed\n")
