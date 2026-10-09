"""Fictional company dates test formulas, not a company's real legal obligations."""

from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime

import pytest
from coinpup_api.reminders.catalog import CHECKED_ON, RULES, VERSION, get_rule
from coinpup_api.reminders.evaluation import (
    ManualDate,
    ReminderEvaluationError,
    ReminderInputs,
    ReminderRule,
    effective_date,
    evaluate,
)


def company(country="CN", **changes):
    base = ReminderInputs(
        entity_kind="company",
        country_code=country,
        company_type="limited_liability" if country == "CN" else "llc",
        region_code="WY" if country == "US" else None,
        registration_date=date(2025, 2, 16),
        filing_year=2026,
        applicability_confirmed=True,
    )
    return replace(base, **changes)


def rule(identifier="cn.company.annual_report"):
    return get_rule(identifier, VERSION)


@pytest.mark.parametrize(
    "country,identifier,expected",
    [
        ("CN", "cn.company.annual_report", date(2026, 6, 30)),
        ("US", "us.wy.llc.annual_report", date(2026, 2, 1)),
    ],
)
def test_report_uses_filing_year_not_anniversary_day(country, identifier, expected):
    inputs = company(country)
    result = evaluate(rule(identifier), inputs)
    assert result.status == "calculated"
    assert result.calculated_date == expected
    assert effective_date(result) == expected
    assert result.inputs == inputs
    assert result.rule.version == VERSION
    assert result.rule.checked_on == CHECKED_ON
    assert result.rule.sources[0].startswith("https://")


@pytest.mark.parametrize(
    "identifier,country",
    [
        ("cn.company.annual_report", "CN"),
        ("us.wy.llc.annual_report", "US"),
    ],
)
@pytest.mark.parametrize("year", [2024, 2025])
def test_first_filing_starts_in_following_year(identifier, country, year):
    result = evaluate(rule(identifier), company(country, filing_year=year))
    assert result.status == "not_applicable"
    assert result.reasons == ("before_first_filing_year",)
    assert effective_date(result) is None


@pytest.mark.parametrize("registration", [date(2024, 2, 29), date(2025, 1, 31), date(2025, 12, 31)])
def test_wy_month_start_handles_leap_and_year_end_without_invoice_clamping(registration):
    result = evaluate(
        rule("us.wy.llc.annual_report"), company("US", registration_date=registration)
    )
    assert result.calculated_date == date(2026, registration.month, 1)


def test_scope_does_not_copy_wy_to_nm_or_personal_entity():
    wy = rule("us.wy.llc.annual_report")
    for inputs in (
        company("US", region_code="NM"),
        company("US", entity_kind="personal"),
        company("US", company_type="corporation"),
        company("CN"),
    ):
        result = evaluate(wy, inputs)
        assert result.status == "not_applicable"
        assert result.reasons == ("scope_mismatch",)
        assert result.calculated_date is None


def test_scope_missing_and_parameter_missing_are_explicit():
    result = evaluate(
        rule("us.wy.llc.annual_report"),
        company(
            "US",
            region_code=None,
            registration_date=None,
            filing_year=None,
        ),
    )
    assert result.status == "missing_parameters"
    assert result.missing == ("filing_year", "region_code", "registration_date")
    assert effective_date(result) is None


def test_explicit_applicability_is_required_and_false_is_not_an_unknown():
    unconfirmed = evaluate(rule(), company(applicability_confirmed=None))
    declined = evaluate(rule(), company(applicability_confirmed=False))
    assert unconfirmed.status == "needs_verification"
    assert unconfirmed.reasons == ("applicability_unconfirmed",)
    assert declined.status == "not_applicable"
    assert declined.reasons == ("applicability_declined",)
    assert effective_date(unconfirmed) is None


@pytest.mark.parametrize(
    "identifier,inputs",
    [
        ("us.nm.llc.annual_report", company("US", region_code="NM")),
        ("hk.private.annual_return", company("HK", company_type="private_limited")),
        (
            "ee.private.annual_report",
            company("EE", company_type="private_limited", period_end=date(2025, 12, 31)),
        ),
    ],
)
def test_incomplete_official_rule_never_silently_generates_a_deadline(identifier, inputs):
    result = evaluate(rule(identifier), inputs)
    assert result.status == "needs_verification"
    assert result.reasons == ("rule_not_verified",)
    assert result.calculated_date is None
    assert effective_date(result) is None


def test_estonia_requires_actual_period_end_not_registration_anniversary():
    result = evaluate(
        rule("ee.private.annual_report"), company("EE", company_type="private_limited")
    )
    assert result.status == "missing_parameters"
    assert result.missing == ("period_end",)


@pytest.mark.parametrize("expiry", [date.min, date(2028, 2, 29), date.max])
def test_expiry_preserves_verified_document_date(expiry):
    result = evaluate(
        rule("certificate.expiry"),
        ReminderInputs(
            expiry_date=expiry,
            applicability_confirmed=True,
        ),
    )
    assert result.status == "calculated"
    assert result.calculated_date == expiry


def fictional_rule(offset=30, version="R-v1"):
    return ReminderRule(
        "test.fictional.anniversary",
        version,
        CHECKED_ON,
        ("fictional-test-fixture",),
        "days_after",
        ("anchor_date",),
        offset_days=offset,
    )


def test_c08_date_and_manual_priority_preserve_original_evaluation_after_rule_update():
    inputs = ReminderInputs(anchor_date=date(2026, 1, 1), applicability_confirmed=True)
    original = evaluate(fictional_rule(), inputs)
    assert original.calculated_date == date(2026, 1, 31)
    override = ManualDate(date(2026, 2, 10), "Fictional accountant confirmed an individual date")
    updated = evaluate(fictional_rule(45, "R-v2"), inputs)
    assert updated.calculated_date == date(2026, 2, 15)
    assert (
        effective_date(original, override) == effective_date(updated, override) == date(2026, 2, 10)
    )
    assert original.rule.version == "R-v1"
    assert original.calculated_date == date(2026, 1, 31)
    with pytest.raises(FrozenInstanceError):
        original.inputs.anchor_date = date(2027, 1, 1)
    with pytest.raises(FrozenInstanceError):
        original.rule.version = "R-v2"


def test_overflow_is_visible_and_manual_fallback_is_explicit():
    result = evaluate(
        fictional_rule(), ReminderInputs(anchor_date=date.max, applicability_confirmed=True)
    )
    assert result.status == "needs_verification"
    assert result.reasons == ("date_out_of_range",)
    assert effective_date(result) is None
    assert (
        effective_date(result, ManualDate(date.max, "Fictional manually verified date")) == date.max
    )


@pytest.mark.parametrize(
    "identifier,version",
    [
        ("unknown", VERSION),
        ("cn.company.annual_report", "latest"),
        ("cn.company.annual_report", "2026-10-09.1"),
    ],
)
def test_unknown_rule_versions_are_rejected(identifier, version):
    with pytest.raises(ReminderEvaluationError, match="reminder_rule_version_unknown"):
        get_rule(identifier, version)


def test_registry_has_unique_immutable_version_keys():
    assert len({(item.id, item.version) for item in RULES}) == len(RULES)
    assert not any(item.id.startswith("test.") for item in RULES)
    assert all(isinstance(item.sources, tuple) for item in RULES)


@pytest.mark.parametrize("year", [True, 0, 10000, 2026.0, "2026"])
def test_year_input_has_no_coercion(year):
    with pytest.raises(ReminderEvaluationError, match="reminder_filing_year"):
        ReminderInputs(filing_year=year)


@pytest.mark.parametrize("field", ["registration_date", "period_end", "expiry_date", "anchor_date"])
@pytest.mark.parametrize("value", ["2026-01-01", datetime(2026, 1, 1)])
def test_dates_do_not_coerce_strings_or_implicit_timezone(field, value):
    with pytest.raises(ReminderEvaluationError, match="reminder_input_date"):
        ReminderInputs(**{field: value})


@pytest.mark.parametrize("value", [1, "true", 0])
def test_applicability_requires_boolean(value):
    with pytest.raises(ReminderEvaluationError, match="reminder_applicability"):
        ReminderInputs(applicability_confirmed=value)


@pytest.mark.parametrize("reason", ["", "  ", "x" * 501, None])
def test_manual_override_requires_reason(reason):
    with pytest.raises(ReminderEvaluationError, match="reminder_manual_reason"):
        ManualDate(date(2026, 1, 1), reason)


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"formula": "eval"}, "reminder_rule_formula"),
        ({"required": ()}, "reminder_rule_formula"),
        ({"required": ("anchor_date", "anchor_date")}, "reminder_rule_parameters"),
        ({"required": ("anything",)}, "reminder_rule_parameters"),
        ({"offset_days": True}, "reminder_rule_offset"),
        ({"offset_days": -1}, "reminder_rule_offset"),
        ({"offset_days": 3661}, "reminder_rule_offset"),
        ({"sources": ()}, "reminder_rule_sources"),
        ({"sources": ["mutable"]}, "reminder_rule_sources"),
    ],
)
def test_bad_rule_configuration_fails_explicitly(changes, code):
    with pytest.raises(ReminderEvaluationError, match=code):
        replace(fictional_rule(), **changes)


@pytest.mark.parametrize("changes", [{"region_code": "NM"}, {"country_code": None}])
def test_wy_formula_cannot_be_misconfigured_to_another_jurisdiction(changes):
    with pytest.raises(ReminderEvaluationError, match="reminder_rule_scope"):
        replace(rule("us.wy.llc.annual_report"), **changes)
