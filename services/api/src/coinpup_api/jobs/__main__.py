"""Explicit maintenance commands; recurring invoices only create drafts."""

import argparse
import json
import sys
from datetime import UTC, date, datetime
from functools import partial
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from coinpup_api.business.recurring_job import run_recurring
from coinpup_api.config import Settings
from coinpup_api.database import Database
from coinpup_api.ledger.service import LedgerError
from coinpup_api.quotes.market import MarketQuoteService
from coinpup_api.reminders.delivery_job import run_reminder_delivery
from coinpup_api.reminders.mail_job import CAPTURE_SENDER, capture_connection, run_reminder_email
from coinpup_api.reminders.mail_transport import SmtpSettings, verified_connection


def limit(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use a whole number from 1 to 100") from None
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("Use a whole number from 1 to 100")
    return number


def _mail_transport(settings):
    if settings.environment == "production":
        smtp = SmtpSettings()
        if smtp.enabled:
            return {"sender": smtp.sender, "connect": partial(verified_connection, smtp)}
    elif settings.reminder_mail_capture_enabled:
        return {
            "sender": CAPTURE_SENDER,
            "connect": partial(capture_connection, settings.reminder_mail_capture_port),
        }
    return {}


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
    email = commands.add_parser(
        "reminder-email", help="Process scoped reminders via configured email"
    )
    email.add_argument("--owner", type=UUID, required=True)
    email.add_argument("--ledger", type=UUID, required=True)
    email.add_argument("--page-size", type=limit, default=50)
    market = commands.add_parser(
        "market-quotes", help="Refresh one configured owner and direct public market pair"
    )
    market.add_argument("--owner", type=UUID, required=True)
    market.add_argument("--asset", required=True)
    market.add_argument("--display", choices=("USD", "GBP", "EUR", "HKD", "CNY"), required=True)
    market.add_argument("--price-date", type=date.fromisoformat)
    delivery = commands.add_parser(
        "reminder-delivery", help="Process owned inbox and optionally configured email"
    )
    delivery.add_argument("--owner", type=UUID, required=True)
    delivery.add_argument("--ledger", type=UUID, help="Restrict the pass to one ledger")
    delivery.add_argument("--page-size", type=limit, default=50)
    args = parser.parse_args(argv)
    database = None
    try:
        settings = Settings()
        if args.job == "market-quotes":
            if not settings.market_quotes_enabled:
                print("Public quote refresh is disabled.", file=sys.stderr)
                return 1
            database = Database(settings)
            observation = MarketQuoteService(database.engine).refresh(
                args.owner, args.asset, args.display, price_date=args.price_date
            )
            print(json.dumps({"observation_id": str(observation) if observation else None}))
            return 0
        email_options = {}
        email_blocked = False
        if args.job in {"reminder-email", "reminder-delivery"}:
            try:
                email_options = _mail_transport(settings)
            except ValueError:
                print("Reminder SMTP configuration is invalid. Email is disabled.", file=sys.stderr)
                email_blocked = True
            if args.job == "reminder-email" and not email_options:
                if not email_blocked:
                    print("Local reminder email capture is disabled.", file=sys.stderr)
                return 1
        database = Database(settings)
        if args.job == "reminder-email":
            result = run_reminder_email(
                database.engine,
                args.owner,
                args.ledger,
                page_size=args.page_size,
                **email_options,
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.job == "reminder-delivery":
            result = run_reminder_delivery(
                database.engine,
                args.owner,
                ledger=args.ledger,
                page_size=args.page_size,
                **email_options,
            )
            print(json.dumps(result, sort_keys=True))
            return int(email_blocked)
        result = run_recurring(database.engine, now=datetime.now(UTC), per_rule=args.per_rule)
        print(json.dumps(result, sort_keys=True))
        return 1 if result["failures"] else 0
    except KeyboardInterrupt:
        return 130
    except (SQLAlchemyError, LedgerError, OSError, ValueError):
        label = {
            "market-quotes": "Public quote job",
            "reminder-delivery": "Reminder delivery job",
            "reminder-email": "Reminder email job",
            "recurring-invoices": "Recurring job",
        }[args.job]
        print(f"{label} unavailable; check configuration and database access.", file=sys.stderr)
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
