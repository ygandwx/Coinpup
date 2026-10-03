"""Restore a verified database-and-files bundle into a new isolated database and storage root."""

import argparse
import os
import sys
from pathlib import Path

import psycopg
from _bundle_archive import (
    child_directory,
    copy_verified,
    new_directory,
    new_file,
    open_directory,
    regular_file,
    stored_file_rows,
    verified_bundle,
)
from _database_archive import (
    ArchiveError,
    empty_target_guard,
    read_target,
    require_posix,
    run_tool,
    tool_environment,
    validate_disposable_target,
)


def restore_bundle(target, directory: Path, storage_root: Path, confirmation: str):
    require_posix()
    validate_disposable_target(target, confirmation)
    # Reject even an existing empty root. This command never merges, overwrites or cleans it.
    with open_directory(storage_root.parent) as parent:
        try:
            os.stat(storage_root.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ArchiveError("Restore storage root already exists; choose a new isolated path.")
    try:
        with (
            verified_bundle(directory) as (manifest, dump, source_blobs),
            tool_environment(target) as environment,
        ):
            run_tool(["pg_restore", "--list"], environment=environment, stdin=dump)
            dump.seek(0)
            with empty_target_guard(target):
                with (
                    new_directory(storage_root) as storage,
                    child_directory(storage, "blobs", create=True) as blobs,
                ):
                    for key, expected in manifest["blobs"].items():
                        with (
                            regular_file(source_blobs, key) as source,
                            new_file(blobs, key) as destination,
                        ):
                            copy_verified(source, destination, expected)
                    os.fsync(blobs)
                    with child_directory(storage, "staging", create=True):
                        pass
                    os.fsync(storage)
                    run_tool(
                        [
                            "pg_restore",
                            "--dbname=" + target.database,
                            "--format=custom",
                            "--no-password",
                            "--single-transaction",
                            "--exit-on-error",
                            "--no-owner",
                            "--no-privileges",
                        ],
                        environment=environment,
                        stdin=dump,
                    )
                    with target.connect() as connection:
                        if stored_file_rows(connection) != manifest["stored_files"]:
                            raise ArchiveError(
                                "Restored file metadata does not match the bundle snapshot."
                            )
    except psycopg.Error:
        raise ArchiveError(
            "Bundle database restore verification failed; no success was recorded."
        ) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backup", required=True, type=Path, help="Completed database-and-files bundle"
    )
    parser.add_argument(
        "--storage-root", required=True, type=Path, help="New isolated private storage root"
    )
    parser.add_argument("--confirm-empty-database", required=True)
    arguments = parser.parse_args(argv)
    try:
        restore_bundle(
            read_target("COINPUP_RESTORE_DATABASE_URL"),
            arguments.backup,
            arguments.storage_root,
            arguments.confirm_empty_database,
        )
    except ArchiveError as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print("Bundle filesystem operation failed; no success was recorded.", file=sys.stderr)
        return 1
    print(
        "Bundle restored and file metadata verified. "
        "Verify the isolated application before any manual cutover."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
