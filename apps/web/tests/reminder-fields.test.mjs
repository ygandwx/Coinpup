import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";
registerHooks({
    resolve(specifier, context, next) {
        return next(
            specifier.startsWith(".") &&
                !/\.[a-z]+$/u.test(specifier) &&
                context.parentURL?.includes("/src/")
                ? specifier + ".ts"
                : specifier,
            context,
        );
    },
});
const fields = await import("../src/reminder-fields.ts");
const annual = {
    id: "cn.company.annual_report",
    version: "Fictional-v1",
    formula: "cn_annual",
    checked_on: "2026-01-01",
    required: ["registration_date", "filing_year"],
    sources: ["Fictional test source"],
    country_code: "CN",
    region_code: null,
    company_type: "limited_liability",
    offset_days: 0,
};
const certificate = {
    ...annual,
    id: "certificate.expiry",
    formula: "expiry",
    required: ["expiry_date"],
    country_code: null,
    company_type: null,
};
const rules = [annual, certificate];
const annualFields = () => ({
    ...fields.reminderFields(),
    title: " Fictional 年审 ",
    ruleKey: fields.reminderRuleKey(annual),
    filingYear: "2027",
});

test("new forms never guess a deadline, year or applicability", () => {
    const input = fields.reminderFields();
    assert.equal(input.filingYear, "");
    assert.equal(input.manualDate, "");
    assert.equal(input.applicability, "");
    const body = fields.createReminderBody("fictional-id", annualFields(), rules);
    assert.equal(body.title, "Fictional 年审");
    assert.equal(body.rule.filing_year, 2027);
    assert.equal(body.rule.applicability_confirmed, null);
    assert.equal(body.manual_due_date, null);
    for (const [choice, expected] of [
        ["yes", true],
        ["no", false],
        ["", null],
    ])
        assert.equal(
            fields.reminderSelection({ ...annualFields(), applicability: choice }, rules)
                .applicability_confirmed,
            expected,
        );
});

test("calendar inputs and explicit rule versions cannot silently change", () => {
    for (const filingYear of ["0", "10000", "-1", "1.5", "1e3", " 2027"])
        assert.throws(
            () => fields.reminderSelection({ ...annualFields(), filingYear }, rules),
            (error) => error.code === "reminder_filing_year",
        );
    assert.throws(
        () => fields.reminderSelection({ ...annualFields(), ruleKey: "missing" }, rules),
        (error) => error.code === "reminder_rule_version_unknown",
    );
    assert.throws(
        () => fields.reminderSelection({ ...annualFields(), kind: "tax" }, rules),
        (error) => error.code === "reminder_rule_kind",
    );
    const input = {
        ...annualFields(),
        kind: "certificate",
        ruleKey: fields.reminderRuleKey(certificate),
        expiryDate: "2028-02-29",
    };
    assert.equal(fields.reminderSelection(input, rules).expiry_date, "2028-02-29");
    assert.equal(fields.reminderSelection(input, rules).filing_year, null);
    for (const expiryDate of ["2027-02-29", "2027-2-1", "2027-02-01T00:00:00Z"])
        assert.throws(
            () => fields.reminderSelection({ ...input, expiryDate }, rules),
            (error) => error.code === "reminder_date",
        );
});

test("metadata save and explicit recalculation use separate bodies", () => {
    assert.deepEqual(fields.editReminderBody(7, annualFields()), {
        expected_version: 7,
        title: "Fictional 年审",
        notes: null,
    });
    const command = fields.recalculateReminderBody(7, annualFields(), rules);
    assert.deepEqual(Object.keys(command).sort(), ["expected_version", "rule"]);
    assert.throws(
        () => fields.recalculateReminderBody(7, fields.reminderFields(), rules),
        (error) => error.code === "reminder_rule_required",
    );
});

test("manual overrides require a fresh reason, including explicit clear", () => {
    const input = fields.reminderFields({
        event_kind: "tax",
        title: "Fictional tax",
        manual_due_date: "2027-02-01",
        manual_reason: "Old fictional reason",
    });
    assert.equal(input.manualDate, "2027-02-01");
    assert.equal(input.manualReason, "");
    for (const clear of [false, true])
        assert.throws(
            () => fields.manualReminderBody(2, input, clear),
            (error) => error.code === "reminder_manual_reason",
        );
    input.manualReason = " Fictional new reason ";
    assert.deepEqual(fields.manualReminderBody(2, input, true), {
        expected_version: 2,
        manual_due_date: null,
        reason: "Fictional new reason",
    });
    assert.deepEqual(fields.manualReminderBody(2, input), {
        expected_version: 2,
        manual_due_date: "2027-02-01",
        reason: "Fictional new reason",
    });
    assert.throws(
        () => fields.createReminderBody("fictional-id", { ...input, manualDate: "" }, rules),
        (error) => error.code === "reminder_manual_reason",
    );
});

test("text limits reject controls without losing multilingual content", () => {
    for (const title of [" ", "x\ny", "x".repeat(161)])
        assert.throws(() => fields.editReminderBody(1, { ...annualFields(), title }));
    for (const notes of ["x\0y", "x".repeat(2001)])
        assert.throws(() => fields.editReminderBody(1, { ...annualFields(), notes }));
    assert.equal(
        fields.editReminderBody(1, { ...annualFields(), notes: "Fictional 中文\nEnglish" }).notes,
        "Fictional 中文\nEnglish",
    );
});
