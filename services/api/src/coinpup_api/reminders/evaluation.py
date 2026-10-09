"""Pure, versioned reminder dates; no clock, database, network or financial effects."""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

Status = Literal["calculated", "missing_parameters", "needs_verification", "not_applicable"]
Formula = Literal["cn_annual", "wy_annual", "expiry", "days_after", "unverified"]


class ReminderEvaluationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ReminderInputs:
    entity_kind: str | None = None
    country_code: str | None = None
    region_code: str | None = None
    company_type: str | None = None
    registration_date: date | None = None
    filing_year: int | None = None
    period_end: date | None = None
    expiry_date: date | None = None
    anchor_date: date | None = None
    applicability_confirmed: bool | None = None

    def __post_init__(self):
        for name in ("registration_date", "period_end", "expiry_date", "anchor_date"):
            value = getattr(self, name)
            if value is not None and type(value) is not date:
                raise ReminderEvaluationError("reminder_input_date")
        if self.filing_year is not None and (
            type(self.filing_year) is not int or not 1 <= self.filing_year <= 9999
        ):
            raise ReminderEvaluationError("reminder_filing_year")
        if (
            self.applicability_confirmed is not None
            and type(self.applicability_confirmed) is not bool
        ):
            raise ReminderEvaluationError("reminder_applicability")
        for name in ("entity_kind", "country_code", "region_code", "company_type"):
            value = getattr(self, name)
            if value is not None and (type(value) is not str or not value or len(value) > 64):
                raise ReminderEvaluationError("reminder_input_scope")


@dataclass(frozen=True)
class ReminderRule:
    """Trusted, shipped metadata; never construct from arbitrary API expressions."""

    id: str
    version: str
    checked_on: date
    sources: tuple[str, ...]
    formula: Formula
    required: tuple[str, ...]
    country_code: str | None = None
    company_type: str | None = None
    region_code: str | None = None
    offset_days: int = 0

    def __post_init__(self):
        if not self.id or not self.version or type(self.checked_on) is not date:
            raise ReminderEvaluationError("reminder_rule_metadata")
        if (
            type(self.sources) is not tuple
            or not self.sources
            or any(type(source) is not str or not source for source in self.sources)
        ):
            raise ReminderEvaluationError("reminder_rule_sources")
        fields = {"registration_date", "filing_year", "period_end", "expiry_date", "anchor_date"}
        if type(self.required) is not tuple or len(set(self.required)) != len(self.required):
            raise ReminderEvaluationError("reminder_rule_parameters")
        if any(name not in fields for name in self.required):
            raise ReminderEvaluationError("reminder_rule_parameters")
        required = {
            "cn_annual": {"registration_date", "filing_year"},
            "wy_annual": {"registration_date", "filing_year"},
            "expiry": {"expiry_date"},
            "days_after": {"anchor_date"},
            "unverified": set(),
        }
        if self.formula not in required or not required[self.formula].issubset(self.required):
            raise ReminderEvaluationError("reminder_rule_formula")
        if type(self.offset_days) is not int or not 0 <= self.offset_days <= 3660:
            raise ReminderEvaluationError("reminder_rule_offset")
        if self.formula != "days_after" and self.offset_days != 0:
            raise ReminderEvaluationError("reminder_rule_offset")
        scope = (self.country_code, self.company_type, self.region_code)
        expected_scope = {
            "cn_annual": ("CN", "limited_liability", None),
            "wy_annual": ("US", "llc", "WY"),
        }
        if self.formula in expected_scope and scope != expected_scope[self.formula]:
            raise ReminderEvaluationError("reminder_rule_scope")
        if self.country_code is None and any(value is not None for value in scope[1:]):
            raise ReminderEvaluationError("reminder_rule_scope")


@dataclass(frozen=True)
class ReminderEvaluation:
    rule: ReminderRule
    inputs: ReminderInputs
    status: Status
    reasons: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    calculated_date: date | None = None


@dataclass(frozen=True)
class ManualDate:
    date: date
    reason: str

    def __post_init__(self):
        if type(self.date) is not date:
            raise ReminderEvaluationError("reminder_manual_date")
        if type(self.reason) is not str or not self.reason.strip() or len(self.reason) > 500:
            raise ReminderEvaluationError("reminder_manual_reason")


def evaluate(rule: ReminderRule, inputs: ReminderInputs) -> ReminderEvaluation:
    """Return evidence, never infer that a company has no other obligations."""
    missing = []
    if rule.country_code is not None:
        scope = {"entity_kind": "company", "country_code": rule.country_code}
        if rule.company_type is not None:
            scope["company_type"] = rule.company_type
        if rule.region_code is not None:
            scope["region_code"] = rule.region_code
        for key, expected in scope.items():
            actual = getattr(inputs, key)
            if actual is None:
                missing.append(key)
            elif actual != expected:
                return ReminderEvaluation(rule, inputs, "not_applicable", ("scope_mismatch",))
    if inputs.applicability_confirmed is False:
        return ReminderEvaluation(rule, inputs, "not_applicable", ("applicability_declined",))
    missing.extend(name for name in rule.required if getattr(inputs, name) is None)
    if missing:
        return ReminderEvaluation(
            rule, inputs, "missing_parameters", ("parameters_required",), tuple(sorted(missing))
        )
    if inputs.applicability_confirmed is not True:
        return ReminderEvaluation(
            rule, inputs, "needs_verification", ("applicability_unconfirmed",)
        )
    if rule.formula == "unverified":
        return ReminderEvaluation(rule, inputs, "needs_verification", ("rule_not_verified",))
    try:
        if rule.formula in ("cn_annual", "wy_annual"):
            # Required parameters have been checked above; target is filing year, not report year.
            assert inputs.registration_date is not None and inputs.filing_year is not None
            if inputs.filing_year <= inputs.registration_date.year:
                return ReminderEvaluation(
                    rule, inputs, "not_applicable", ("before_first_filing_year",)
                )
            month, day = (
                (6, 30) if rule.formula == "cn_annual" else (inputs.registration_date.month, 1)
            )
            calculated = date(inputs.filing_year, month, day)
        elif rule.formula == "expiry":
            assert inputs.expiry_date is not None
            calculated = inputs.expiry_date
        else:
            assert rule.formula == "days_after" and inputs.anchor_date is not None
            calculated = inputs.anchor_date + timedelta(days=rule.offset_days)
    except (ValueError, OverflowError):
        return ReminderEvaluation(rule, inputs, "needs_verification", ("date_out_of_range",))
    return ReminderEvaluation(rule, inputs, "calculated", calculated_date=calculated)


def effective_date(
    evaluation: ReminderEvaluation, override: ManualDate | None = None
) -> date | None:
    """Manual selection survives reevaluation; persistence records its history separately."""
    if override is not None:
        return override.date
    return evaluation.calculated_date if evaluation.status == "calculated" else None
