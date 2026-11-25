"""One explicit scoped pass over current reminder email configurations."""

from collections import Counter
from functools import partial
from smtplib import SMTP

from coinpup_api.reminders.mail_attempt import MailLeaseEnded, attempt_live_mail, single_mailbox
from coinpup_api.reminders.mail_service import ReminderEmailService

CAPTURE_SENDER = "reminders@coinpup.invalid"


def capture_connection(port):
    return SMTP("127.0.0.1", port, timeout=10)


def run_reminder_email(engine, owner, ledger, *, sender, connect, page_size=50):
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise ValueError("The email scan page size must be 1–100.")
    sender = single_mailbox(sender)
    service = ReminderEmailService(engine)
    after = None
    counts = Counter()
    scanned = 0
    while events := service.candidate_events(owner, ledger, after=after, limit=page_size):
        for event in events:
            scanned += 1
            claim = service.claim(owner, ledger, event, sender)
            if claim is None:
                continue

            live = partial(service.is_live, owner, ledger, claim)
            try:
                result = attempt_live_mail(claim.message, connect, live)
            except MailLeaseEnded:
                # The helper did not call SMTP to send. The database still owns the
                # final lease state and marks expired leases conservatively.
                result = None
            view = service.finish(owner, ledger, claim, result)
            counts[view.state] += 1
        after = events[-1]
    return {"scanned": scanned, "outcomes": dict(counts)}
