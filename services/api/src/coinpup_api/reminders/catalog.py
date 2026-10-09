"""Versioned source registry. Unknown versions never fall forward to a newer rule."""

from datetime import date

from coinpup_api.reminders.evaluation import ReminderEvaluationError, ReminderRule

# Keep published definitions when adding versions; never relabel or replace old entries.
VERSION = "2026-10-10.1"
CHECKED_ON = date(2026, 10, 10)
RULES = (
    ReminderRule(
        "cn.company.annual_report",
        VERSION,
        CHECKED_ON,
        ("https://www.samr.gov.cn/xyjgs/flfg/art/2024/art_be55c2e3a54a43e5ab12794c9dc87600.html",),
        "cn_annual",
        ("registration_date", "filing_year"),
        "CN",
        "limited_liability",
    ),
    ReminderRule(
        "us.wy.llc.annual_report",
        VERSION,
        CHECKED_ON,
        ("https://sos.wyo.gov/Forms/WyoBiz/What%27s_Next.pdf",),
        "wy_annual",
        ("registration_date", "filing_year"),
        "US",
        "llc",
        "WY",
    ),
    ReminderRule(
        "hk.private.annual_return",
        VERSION,
        CHECKED_ON,
        ("https://www.cr.gov.hk/en/faq/local-company/annual-return.htm",),
        "unverified",
        ("registration_date", "filing_year"),
        "HK",
        "private_limited",
    ),
    ReminderRule(
        "ee.private.annual_report",
        VERSION,
        CHECKED_ON,
        ("https://www.rik.ee/en/e-business-register/annual-report",),
        "unverified",
        ("period_end",),
        "EE",
        "private_limited",
    ),
    ReminderRule(
        "us.nm.llc.annual_report",
        VERSION,
        CHECKED_ON,
        ("https://www.sos.nm.gov/business-services/statutes-governing-business-in-nm/",),
        "unverified",
        (),
        "US",
        "llc",
        "NM",
    ),
    ReminderRule(
        "certificate.expiry",
        VERSION,
        CHECKED_ON,
        ("user_verified_certificate_expiry",),
        "expiry",
        ("expiry_date",),
    ),
)


def get_rule(rule_id: str, version: str) -> ReminderRule:
    for rule in RULES:
        if rule.id == rule_id and rule.version == version:
            return rule
    raise ReminderEvaluationError("reminder_rule_version_unknown")
