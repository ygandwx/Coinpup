"""Back up one PostgreSQL snapshot and exactly its referenced private immutable blobs."""

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from _bundle_archive import (
    MAX_MANIFEST_BYTES,
    child_directory,
    copy_verified,
    file_descriptors,
    new_directory,
    new_file,
    open_directory,
    regular_file,
    stored_file_rows,
    sync_output,
)
from _database_archive import (
    ArchiveError,
    digest_file,
    read_target,
    require_posix,
    run_tool,
    tool_environment,
)


def backup_bundle(target, storage_root: Path, directory: Path):
    require_posix()
    try:
        with open_directory(storage_root) as storage:
            with (
                new_directory(directory) as output,
                child_directory(output, "blobs", create=True) as blobs,
            ):
                with target.connect() as connection:
                    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    version = int(connection.execute("SHOW server_version_num").fetchone()[0])
                    if not 170000 <= version < 180000:
                        raise ArchiveError(
                            "Bundle backup requires PostgreSQL 17 and matching client tools."
                        )
                    snapshot = connection.execute("SELECT pg_export_snapshot()").fetchone()[0]
                    if (
                        not isinstance(snapshot, str)
                        or re.fullmatch(r"[0-9A-Fa-f-]+", snapshot) is None
                    ):
                        raise ArchiveError("PostgreSQL returned an invalid snapshot identifier.")
                    rows = stored_file_rows(connection)
                    descriptors = file_descriptors(rows)
                    with (
                        tool_environment(target) as environment,
                        new_file(output, "database.dump") as dump,
                    ):
                        run_tool(
                            [
                                "pg_dump",
                                "--format=custom",
                                "--no-password",
                                "--no-owner",
                                "--no-privileges",
                                "--snapshot=" + snapshot,
                            ],
                            environment=environment,
                            stdout=dump,
                        )
                        sync_output(dump)
                    # The exporting transaction stays alive until pg_dump has imported and
                    # consumed the snapshot. Immutable referenced blobs outlive this transaction.
                with regular_file(output, "database.dump") as dump:
                    if dump.read(5) != b"PGDMP":
                        raise ArchiveError("pg_dump did not produce a PostgreSQL custom archive.")
                    digest, size = digest_file(dump)
                if descriptors:
                    with child_directory(storage, "blobs") as source_blobs:
                        for key, expected in descriptors.items():
                            with (
                                regular_file(source_blobs, key) as source,
                                new_file(blobs, key) as destination,
                            ):
                                copy_verified(source, destination, expected)
                os.fsync(blobs)
                manifest = {
                    "version": 2,
                    "format": "postgresql-custom",
                    "scope": "database-and-files",
                    "file": "database.dump",
                    "sha256": digest,
                    "bytes": size,
                    "created_at": datetime.now(UTC).isoformat(),
                    "stored_files": rows,
                    "blobs": descriptors,
                }
                encoded = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
                if len(encoded) > MAX_MANIFEST_BYTES:
                    raise ArchiveError(
                        "Bundle metadata exceeds the supported 64 MiB manifest size."
                    )
                # Last and durable: absence of this file always means an incomplete backup.
                with new_file(output, "manifest.pending") as destination:
                    destination.write(encoded)
                    sync_output(destination)
                os.link(
                    "manifest.pending",
                    "manifest.json",
                    src_dir_fd=output,
                    dst_dir_fd=output,
                    follow_symlinks=False,
                )
                os.unlink("manifest.pending", dir_fd=output)
                os.fsync(output)
    except psycopg.Error:
        raise ArchiveError("Database snapshot backup failed; no success was recorded.") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--storage-root", required=True, type=Path, help="Existing private storage root"
    )
    parser.add_argument(
        "--output", required=True, type=Path, help="New bundle directory; no overwrite"
    )
    arguments = parser.parse_args(argv)
    try:
        backup_bundle(
            read_target("COINPUP_BACKUP_DATABASE_URL"), arguments.storage_root, arguments.output
        )
    except FileExistsError:
        print(
            "Backup output exists; choose a new directory. Nothing was overwritten.",
            file=sys.stderr,
        )
        return 1
    except ArchiveError as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print("Bundle filesystem operation failed; no success was recorded.", file=sys.stderr)
        return 1
    print("Database-and-files bundle completed; all referenced blobs verified with SHA-256.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
