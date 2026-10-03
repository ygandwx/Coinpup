"""Restore a verified archive only into an explicitly confirmed, empty disposable database."""

import argparse
import sys
from pathlib import Path

from _database_archive import (
    ArchiveError,
    empty_target_guard,
    read_target,
    require_posix,
    run_tool,
    tool_environment,
    validate_disposable_target,
    verified_archive,
)


def restore_database(target, directory: Path, confirmation: str):
    require_posix()
    validate_disposable_target(target, confirmation)
    with verified_archive(directory) as source, tool_environment(target) as environment:
        # Validate archive readability before connecting to the target.
        run_tool(["pg_restore", "--list"], environment=environment, stdin=source)
        source.seek(0)
        with empty_target_guard(target):
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
                stdin=source,
            )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", required=True, type=Path, help="Completed backup directory")
    parser.add_argument(
        "--confirm-empty-database", required=True, help="Exact disposable target name"
    )
    arguments = parser.parse_args(argv)
    try:
        require_posix()
        target = read_target("COINPUP_RESTORE_DATABASE_URL")
        restore_database(target, arguments.backup, arguments.confirm_empty_database)
    except ArchiveError as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print("Restore filesystem operation failed; no success was recorded.", file=sys.stderr)
        return 1
    print(
        "Database restored into the confirmed disposable target. Verify before any manual cutover."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
