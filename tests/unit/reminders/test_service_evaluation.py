"""The service selects shipped rules and captures server-owned company facts."""

from datetime import date
from types import SimpleNamespace

import pytest
from coinpup_api.ledger.service import LedgerError
from coinpup_api.reminders.catalog import VERSION
from coinpup_api.reminders.schemas import RuleSelection
from coinpup_api.reminders.service import ReminderService


def company(country="CN", region=None, company_type="limited_liability"):
    return SimpleNamespace(
        kind="company",
        country_code=country,
        region_code=region,
        company_type=company_type,
        registration_date=date(2020, 3, 1),
    )


def test_scope_mismatch_is_retained_without_a_fabricated_deadline():
    selection = RuleSelection(
        rule_id="us.wy.llc.annual_report",
        rule_version=VERSION,
        filing_year=2027,
        applicability_confirmed=True,
    )
    result = ReminderService._evaluate(company("US", "NM", "llc"), "annual", selection)
    assert result["evaluation_status"] == "not_applicable"
    assert result["calculated_date"] is None
    assert result["evaluation"]["inputs"]["region_code"] == "NM"


@pytest.mark.parametrize(
    "kind,version,code",
    [
        ("tax", VERSION, "reminder_rule_kind"),
        ("annual", "missing", "reminder_rule_version_unknown"),
    ],
)
def test_unknown_rules_and_wrong_event_kind_are_rejected(kind, version, code):
    selection = RuleSelection(rule_id="cn.company.annual_report", rule_version=version)
    with pytest.raises(LedgerError) as failure:
        ReminderService._evaluate(company(), kind, selection)
    assert failure.value.code == code and failure.value.status == 422


def test_calculation_captures_rule_evidence_as_json_calendar_dates():
    selection = RuleSelection(
        rule_id="cn.company.annual_report",
        rule_version=VERSION,
        filing_year=2027,
        applicability_confirmed=True,
    )
    result = ReminderService._evaluate(company(), "annual", selection)
    assert result["evaluation"]["rule"]["checked_on"] == "2026-10-10"
    assert result["evaluation"]["inputs"]["registration_date"] == "2020-03-01"
    assert result["evaluation"]["calculated_date"] == "2027-06-30"
    assert result["calculated_date"] == date(2027, 6, 30)
