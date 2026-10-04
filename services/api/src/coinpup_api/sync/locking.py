"""One transaction lock orders single-owner business writes before cursor allocation."""

from sqlalchemy import text
from sqlalchemy.orm import Session

WRITE_LOCK_KEY = 18945999704708432


def acquire_write_lock(session: Session) -> None:
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": WRITE_LOCK_KEY})
