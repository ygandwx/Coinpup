"""Local administrator lifecycle; no public registration or plaintext CLI passwords."""

import argparse
import getpass
import sys
import uuid
import warnings

from sqlalchemy import Engine, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from coinpup_api.auth import AuthError, lock_login_guard
from coinpup_api.config import Settings
from coinpup_api.database import Database
from coinpup_api.models import Administrator, AuthSession
from coinpup_api.security import hash_password, normalize_username


def create_admin(engine: Engine, username: str, password: str) -> uuid.UUID:
    username = normalize_username(username)
    encoded = hash_password(password)
    with Session(engine) as session, session.begin():
        lock_login_guard(session)
        if session.scalar(select(Administrator.id)) is not None:
            raise ValueError("An administrator already exists; use reset-password if needed")
        identifier = uuid.uuid4()
        session.add(
            Administrator(id=identifier, singleton=1, username=username, password_hash=encoded)
        )
    return identifier


def reset_admin_password(engine: Engine, password: str) -> None:
    encoded = hash_password(password)
    with Session(engine) as session, session.begin():
        guard = lock_login_guard(session)
        administrator = session.scalar(select(Administrator).with_for_update())
        if administrator is None:
            raise ValueError("No administrator exists; use create first")
        now = session.scalar(select(func.clock_timestamp()))
        administrator.password_hash = encoded
        administrator.updated_at = now
        session.execute(update(AuthSession).values(revoked_at=now))
        guard.failure_count = 0
        guard.window_started_at = None
        guard.locked_until = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Initialize or reset Coinpup's single administrator"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    create = subcommands.add_parser("create", help="Create the first and only administrator")
    create.add_argument("--username", required=True)
    subcommands.add_parser("reset-password", help="Reset the password and revoke all sessions")
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        parser.error("Run in an interactive terminal; passwords are never accepted via arguments")
    database = None
    try:
        with warnings.catch_warnings():
            # getpass otherwise falls back to echoed input if terminal control fails.
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("New password (12–128 characters): ")
            confirmation = getpass.getpass("Repeat new password: ")
        if password != confirmation:
            raise ValueError("Passwords do not match")
        database = Database(Settings())
        if args.command == "create":
            create_admin(database.engine, args.username, password)
            print("Administrator created.")
        else:
            reset_admin_password(database.engine, password)
            print("Password reset. All previous sessions have been revoked.")
        return 0
    except (ValueError, AuthError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print(
            "Database operation failed. Check configuration and apply migrations first.",
            file=sys.stderr,
        )
        return 1
    except getpass.GetPassWarning:
        print("A terminal with hidden password input is required.", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Cancelled.", file=sys.stderr)
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
