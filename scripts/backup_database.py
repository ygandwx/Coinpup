"""Create one new database-only backup; credentials come exclusively from the environment."""

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from _database_archive import (
    ArchiveError,
    create_private_directory,
    digest_file,
    private_file,
    read_target,
    require_posix,
    run_tool,
    tool_environment,
)


def backup_database(target, directory: Path):
    create_private_directory(directory)
    dump_path = directory / "database.dump"
    with tool_environment(target) as environment, private_file(dump_path) as output:
        run_tool(
            ["pg_dump", "--format=custom", "--no-password", "--no-owner", "--no-privileges"],
            environment=environment,
            stdout=output,
        )
        output.flush()
        os.fsync(output.fileno())
    with dump_path.open("rb") as source:
        if source.read(5) != b"PGDMP":
            raise ArchiveError("pg_dump did not produce a PostgreSQL custom archive.")
        digest, size = digest_file(source)
    manifest = {
        "version": 1,
        "format": "postgresql-custom",
        "scope": "database-only",
        "file": "database.dump",
        "sha256": digest,
        "bytes": size,
        "created_at": datetime.now(UTC).isoformat(),
    }
    # The manifest is the completion marker; failed dumps never receive it.
    with private_file(directory / "manifest.json") as output:
        output.write((json.dumps(manifest, indent=2) + "\n").encode())
        output.flush()
        os.fsync(output.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", required=True, type=Path, help="New backup directory; no overwrite"
    )
    arguments = parser.parse_args(argv)
    try:
        require_posix()
        target = read_target("COINPUP_BACKUP_DATABASE_URL")
        backup_database(target, arguments.output)
    except FileExistsError:
        print(
            "Backup output already exists; choose a new directory. Nothing was overwritten.",
            file=sys.stderr,
        )
        return 1
    except ArchiveError as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print("Backup filesystem operation failed; no success was recorded.", file=sys.stderr)
        return 1
    print("Database-only backup completed with SHA-256 manifest. Attachments are not included.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
