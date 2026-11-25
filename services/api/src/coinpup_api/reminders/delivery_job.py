"""Run explicitly scoped in-app and optional email channels without mixing their outcomes."""

from collections import Counter

from coinpup_api.reminders.inbox_service import ReminderInboxService
from coinpup_api.reminders.mail_job import CAPTURE_SENDER, run_reminder_email


def _ledgers(service, owner, ledger, page_size):
    if ledger is not None:
        yield ledger
        return
    after = None
    while ledgers := service.owned_ledgers(owner, after=after, limit=page_size):
        yield from ledgers
        after = ledgers[-1]


def run_reminder_delivery(
    engine, owner, *, ledger=None, connect=None, sender=CAPTURE_SENDER, page_size=50
):
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise ValueError("The delivery scan page size must be 1–100.")
    service = ReminderInboxService(engine)
    ledger_count = inbox_scanned = inbox_delivered = mail_scanned = 0
    mail_outcomes = Counter()
    for ledger_id in _ledgers(service, owner, ledger, page_size):
        ledger_count += 1
        after = None
        while events := service.candidate_events(owner, ledger_id, after=after, limit=page_size):
            for event in events:
                inbox_scanned += 1
                inbox_delivered += service.deliver(owner, ledger_id, event)
            after = events[-1]
        if connect is not None:
            mail = run_reminder_email(
                engine,
                owner,
                ledger_id,
                sender=sender,
                connect=connect,
                page_size=page_size,
            )
            mail_scanned += mail["scanned"]
            mail_outcomes.update(mail["outcomes"])
    return {
        "ledgers": ledger_count,
        "inbox": {"scanned": inbox_scanned, "delivered": inbox_delivered},
        "email": {"scanned": mail_scanned, "outcomes": dict(mail_outcomes)}
        if connect is not None
        else None,
    }
