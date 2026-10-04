"""Pinned candidate build inputs and actual Linux package/build evidence."""

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

MANIFEST = Path(__file__).with_name("build-inputs.json")


def manifest():
    return json.loads(MANIFEST.read_text(encoding="utf8"))


def record(path):
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": str(path), "byte_size": path.stat().st_size, "sha256": digest}


def safe(path):
    if any(p.is_symlink() or p.is_junction() for p in [path, *path.absolute().parents]):
        raise ValueError("build_input_invalid")
    return path


def verify(path, expected):
    safe(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("build_input_invalid")
    actual = record(path)
    if any(actual[key] != expected[key] for key in ("byte_size", "sha256")):
        raise ValueError("build_input_invalid")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise ValueError("build_input_redirect")


def source_cache(directory, *, fetch=False):
    safe(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for item in manifest()["sources"]:
        if item["cache"] != "build":
            continue
        target = directory / item["name"]
        if not target.exists() and fetch:
            temporary = None
            try:
                if item["url"] != (
                    "https://codeload.github.com/DanBloomberg/leptonica/zip/"
                    "dbb48b0fd0e10e943ecd9bab7439701842894733"
                ):
                    raise ValueError("build_input_invalid")
                fd, temporary = tempfile.mkstemp(prefix=".source-", dir=directory)
                deadline, size = time.monotonic() + 180, 0
                with os.fdopen(fd, "wb") as output:
                    with urllib.request.build_opener(NoRedirect()).open(
                        item["url"], timeout=30
                    ) as response:
                        while True:
                            data = response.read1(min(65536, item["byte_size"] - size + 1))
                            if time.monotonic() >= deadline:
                                raise ValueError("build_input_timeout")
                            if not data:
                                break
                            size += len(data)
                            if size > item["byte_size"]:
                                raise ValueError("build_input_invalid")
                            output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                verify(Path(temporary), item)
                try:
                    os.link(temporary, target, follow_symlinks=False)
                except FileExistsError:
                    verify(target, item)
            finally:
                if temporary is not None:
                    Path(temporary).unlink(missing_ok=True)
        verify(target, item)


def unpack(assets, sources, output):
    """Only the two SHA-verified source archives; never archive extraction to arbitrary paths."""
    safe(output)
    output.mkdir(parents=True, exist_ok=False)
    for item in manifest()["sources"]:
        archive_path = (sources if item["cache"] == "build" else assets) / item["name"]
        verify(archive_path, item)
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if len(entries) > 10000 or sum(i.file_size for i in entries) > 128 * 1024**2:
                raise ValueError("build_source_limit")
            for entry in entries:
                parts = PurePosixPath(entry.filename).parts
                if (
                    not parts
                    or parts[0] != item["archive_root"]
                    or ".." in parts
                    or "\\" in entry.filename
                    or stat.S_ISLNK(entry.external_attr >> 16)
                ):
                    raise ValueError("build_source_invalid")
                target = output / item["product"] / Path(*parts[1:])
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as source, target.open("xb") as destination:
                        shutil.copyfileobj(source, destination, length=65536)


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=120).stdout


def save(path, data):
    path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf8")


def system_inventory(debs, output):
    output.mkdir(parents=True, exist_ok=False)
    definitions = manifest()
    installed = run(
        "dpkg-query",
        "-W",
        "-f=${binary:Package}\t${Version}\t${Architecture}\t${source:Package}\t${source:Version}\n",
    )
    packages = []
    for line in installed.splitlines():
        name, version, architecture, source, source_version = line.split("\t")
        copyright_path = Path("/usr/share/doc") / name.split(":")[0] / "copyright"
        notice = output / "notices" / name.replace(":", "_") / "copyright"
        notice.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(copyright_path, notice)
        packages.append(
            {
                "name": name,
                "version": version,
                "architecture": architecture,
                "source_package": source,
                "source_version": source_version,
                "copyright": record(copyright_path),
                "notice": str(notice.relative_to(output)),
                "files": run("dpkg-query", "-L", name).splitlines(),
            }
        )
    archives = []
    for path in sorted(debs.glob("*.deb")):
        fields = run("dpkg-deb", "-f", str(path), "Package", "Version", "Architecture").splitlines()
        metadata = dict(field.split(": ", 1) for field in fields)
        archives.append(
            dict(
                record(path),
                package=metadata["Package"],
                version=metadata["Version"],
                architecture=metadata["Architecture"],
            )
        )
    actual_versions = {p["name"].split(":")[0]: p["version"] for p in packages}
    for expected in definitions["apt"]["packages"]:
        if actual_versions.get(expected["name"]) != expected["version"]:
            raise ValueError("installed_package_mismatch")
        matches = [d for d in archives if d["package"] == expected["name"]]
        if matches and any(d["sha256"] != expected["sha256"] for d in matches):
            raise ValueError("downloaded_package_mismatch")
    shutil.copytree("/usr/share/common-licenses", output / "common-licenses", symlinks=False)
    python_license = Path("/usr/local/lib/python3.12/LICENSE.txt")
    shutil.copyfile(python_license, output / "python-LICENSE.txt")
    save(
        output / "packages.json",
        {
            "version": 1,
            "packages": packages,
            "debs": archives,
            "snapshot": definitions["apt"]["snapshot"],
            "python_license": record(python_license),
            "base_image": definitions["base_image"],
            "common_licenses": [
                record(p) for p in sorted((output / "common-licenses").iterdir()) if p.is_file()
            ],
        },
    )
    shutil.copyfile(MANIFEST, output / "build-inputs.json")


def build_record(source, lept_build, tess_build, output):
    output.mkdir(parents=True, exist_ok=False)
    for product, build in (("leptonica", lept_build), ("tesseract", tess_build)):
        shutil.copyfile(build / "CMakeCache.txt", output / (product + "-CMakeCache.txt"))
        license_name = "leptonica-license.txt" if product == "leptonica" else "LICENSE"
        shutil.copyfile(source / product / license_name, output / (product + "-LICENSE"))
    binary = "/opt/tesseract/bin/tesseract"
    versions = {
        "tesseract": run(binary, "--version"),
        "compiler": run("c++", "--version"),
        "cmake": run("cmake", "--version"),
        "python": run("python", "--version"),
    }
    native = sorted({p.resolve() for p in Path("/opt/tesseract/lib").glob("*.so*")})
    save(
        output / "sources.json",
        {
            "version": 1,
            "sources": manifest()["sources"],
            "versions": versions,
            "native": [dict(record(p), ldd=run("ldd", str(p))) for p in native],
            "executable": dict(record(Path(binary)), ldd=run("ldd", binary)),
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("fetch", "verify", "unpack", "system", "record", "apt-specs")
    )
    for option in (
        "output-dir",
        "assets-dir",
        "sources-dir",
        "debs-dir",
        "lept-build",
        "tess-build",
    ):
        parser.add_argument("--" + option, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "apt-specs":
            print(" ".join(p["name"] + "=" + p["version"] for p in manifest()["apt"]["packages"]))
            return
        if args.command in ("fetch", "verify"):
            source_cache(args.output_dir, fetch=args.command == "fetch")
        elif args.command == "unpack":
            unpack(args.assets_dir, args.sources_dir, args.output_dir)
        elif args.command == "system":
            system_inventory(args.debs_dir, args.output_dir)
        else:
            build_record(args.sources_dir, args.lept_build, args.tess_build, args.output_dir)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
        zipfile.BadZipFile,
    ):
        parser.exit(1, "candidate_build_input_failed\n")
    print(json.dumps({"version": 1, "command": args.command, "verified": True}))


if __name__ == "__main__":
    main()
