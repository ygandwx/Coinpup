"""Private immutable-blob bundles. All filesystem access is descriptor-relative on POSIX."""

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from _database_archive import ArchiveError, digest_file, require_posix

MAX_MANIFEST_BYTES = 64 * 1024 * 1024
KEY = re.compile(r"[0-9a-f]{32}")
SHA256 = re.compile(r"[0-9a-f]{64}")


@contextmanager
def open_directory(path: Path):
    """Pin every path component; a symlink cannot redirect later relative opens."""
    require_posix()
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def child_directory(parent: int, name: str, *, create=False):
    if create:
        os.mkdir(name, mode=0o700, dir_fd=parent)
        os.fsync(parent)
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def new_directory(path: Path):
    with (
        open_directory(path.parent) as parent,
        child_directory(parent, path.name, create=True) as descriptor,
    ):
        yield descriptor


def regular_file(parent: int, name: str):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ArchiveError("Bundle members must be regular files, never links or special files.")
    return os.fdopen(descriptor, "rb")


def new_file(parent: int, name: str):
    descriptor = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
    )
    return os.fdopen(descriptor, "wb")


def sync_output(output):
    output.flush()
    os.fsync(output.fileno())


def stored_file_rows(connection):
    connection.execute("SET LOCAL TIME ZONE 'UTC'")
    connection.execute("SET LOCAL DateStyle = 'ISO, YMD'")
    return [
        row[0]
        for row in connection.execute(
            "SELECT row_to_json(f)::text FROM public.stored_files f ORDER BY f.id"
        ).fetchall()
    ]


def file_descriptors(rows):
    """Validate all untrusted path material before opening or creating any blob."""
    if not isinstance(rows, list):
        raise ArchiveError("Bundle file metadata is invalid.")
    files, identities = {}, set()
    try:
        for row in rows:
            if not isinstance(row, str):
                raise ValueError
            item = json.loads(row)
            identity = str(UUID(item["id"]))
            UUID(item["ledger_id"])
            UUID(item["created_by"])
            key, digest, size = item["blob_key"], item["sha256"], item["byte_size"]
            if (
                identity in identities
                or not isinstance(key, str)
                or KEY.fullmatch(key) is None
                or key in files
                or not isinstance(digest, str)
                or SHA256.fullmatch(digest) is None
                or type(size) is not int
                or not 0 < size <= 9223372036854775807
            ):
                raise ValueError
            identities.add(identity)
            files[key] = {"sha256": digest, "bytes": size}
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ArchiveError("Bundle file metadata is invalid.") from None
    return files


def copy_verified(source, output, expected):
    digest, size = hashlib.sha256(), 0
    source.seek(0)
    while block := source.read(1024 * 1024):
        size += len(block)
        if size > expected["bytes"]:
            raise ArchiveError("A referenced file has a size or SHA-256 mismatch.")
        digest.update(block)
        output.write(block)
    if size != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
        raise ArchiveError("A referenced file has a size or SHA-256 mismatch.")
    sync_output(output)


def validate_manifest(manifest):
    try:
        if (
            not isinstance(manifest, dict)
            or manifest.get("version") != 2
            or manifest.get("format") != "postgresql-custom"
            or manifest.get("scope") != "database-and-files"
            or manifest.get("file") != "database.dump"
            or not isinstance(manifest.get("sha256"), str)
            or SHA256.fullmatch(manifest["sha256"]) is None
            or type(manifest.get("bytes")) is not int
            or manifest["bytes"] < 5
        ):
            raise ValueError
        descriptors = file_descriptors(manifest["stored_files"])
        if manifest.get("blobs") != descriptors:
            raise ValueError
        return descriptors
    except (KeyError, ValueError, TypeError):
        raise ArchiveError(
            "Bundle manifest is invalid or is not a database-and-files bundle."
        ) from None


@contextmanager
def verified_bundle(directory: Path):
    """Keep the verified dump and directory FDs pinned through restore; recheck blob copies."""
    try:
        with open_directory(directory) as root:
            with regular_file(root, "manifest.json") as source:
                if os.fstat(source.fileno()).st_size > MAX_MANIFEST_BYTES:
                    raise ValueError
                encoded = source.read(MAX_MANIFEST_BYTES + 1)
                if len(encoded) > MAX_MANIFEST_BYTES:
                    raise ValueError
                manifest = json.loads(encoded)
            descriptors = validate_manifest(manifest)
            with (
                regular_file(root, "database.dump") as dump,
                child_directory(root, "blobs") as blobs,
            ):
                if os.fstat(dump.fileno()).st_size != manifest["bytes"]:
                    raise ValueError
                if dump.read(5) != b"PGDMP":
                    raise ValueError
                digest, size = digest_file(dump)
                if digest != manifest["sha256"] or size != manifest["bytes"]:
                    raise ValueError
                # Reject unmanifested files, including staging debris and path substitutions.
                if set(os.listdir(root)) != {"manifest.json", "database.dump", "blobs"}:
                    raise ValueError
                if set(os.listdir(blobs)) != set(descriptors):
                    raise ValueError
                for key, expected in descriptors.items():
                    with regular_file(blobs, key) as source:
                        if os.fstat(source.fileno()).st_size != expected["bytes"]:
                            raise ValueError
                        if digest_file(source) != (expected["sha256"], expected["bytes"]):
                            raise ValueError
                yield manifest, dump, blobs
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ArchiveError("Bundle is incomplete, invalid or has a SHA-256 mismatch.") from None
