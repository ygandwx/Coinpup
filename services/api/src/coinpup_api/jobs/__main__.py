"""Explicit maintenance commands; recurring invoices only create drafts."""

import argparse
import json
import sys
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError

from coinpup_api.business.recurring_job import run_recurring
from coinpup_api.config import Settings
from coinpup_api.database import Database
from coinpup_api.ledger.service import LedgerError


def limit(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use a whole number from 1 to 100") from None
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("Use a whole number from 1 to 100")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a Coinpup maintenance job once")
    commands = parser.add_subparsers(dest="job", required=True)
    recurring = commands.add_parser("recurring-invoices", help="Generate due invoice drafts")
    recurring.add_argument(
        "--per-rule",
        type=limit,
        default=10,
        help="Maximum occurrences per rule in this invocation (1–100)",
    )
    args = parser.parse_args(argv)
    database = None
    try:
        database = Database(Settings())
        result = run_recurring(database.engine, now=datetime.now(UTC), per_rule=args.per_rule)
        print(json.dumps(result, sort_keys=True))
        return 1 if result["failures"] else 0
    except KeyboardInterrupt:
        return 130
    except (SQLAlchemyError, LedgerError, OSError, ValueError):
        print(
            "Recurring job unavailable; check configuration and database access.", file=sys.stderr
        )
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
