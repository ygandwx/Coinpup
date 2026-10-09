import { ApiError } from "./api";
import { validDraftDate } from "./draft-fields";
import { masterName } from "./pending-business";
import type {
    Reminder,
    ReminderCreate,
    ReminderEdit,
    ReminderManual,
    ReminderRecalculate,
    ReminderRule,
} from "./reminder-api";

export type ReminderFields = {
    kind: ReminderCreate["event_kind"];
    title: string;
    notes: string;
    ruleKey: string;
    filingYear: string;
    periodEnd: string;
    expiryDate: string;
    applicability: "" | "yes" | "no";
    manualDate: string;
    manualReason: string;
};
const object = (value: unknown): value is Record<string, unknown> =>
    !!value && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown): string => (typeof value === "string" ? value : "");
const invalid = (code: string): never => {
    throw new ApiError("server", 422, code);
};
export const reminderRuleKey = (rule: Pick<ReminderRule, "id" | "version">) =>
    JSON.stringify([rule.id, rule.version]);

export function reminderFields(row?: Reminder): ReminderFields {
    const evidence = object(row?.evaluation) ? row.evaluation : null;
    const rule = object(evidence?.rule) ? evidence.rule : null;
    const inputs = object(evidence?.inputs) ? evidence.inputs : null;
    return {
        kind: row?.event_kind ?? "annual",
        title: row?.title ?? "",
        notes: row?.notes ?? "",
        ruleKey:
            rule && typeof rule.id === "string" && typeof rule.version === "string"
                ? reminderRuleKey({ id: rule.id, version: rule.version })
                : "",
        filingYear: typeof inputs?.filing_year === "number" ? String(inputs.filing_year) : "",
        periodEnd: text(inputs?.period_end),
        expiryDate: text(inputs?.expiry_date),
        applicability:
            inputs?.applicability_confirmed === true
                ? "yes"
                : inputs?.applicability_confirmed === false
                  ? "no"
                  : "",
        manualDate: row?.manual_due_date ?? "",
        manualReason: "",
    };
}

function boundedName(value: string, max: number, code: string): string {
    const clean = masterName(value);
    if (
        !clean ||
        Array.from(clean).length > max ||
        Array.from(clean).some((char) => char.charCodeAt(0) < 32)
    )
        return invalid(code);
    return clean;
}
function calendar(value: string): string | null {
    if (!value) return null;
    if (!validDraftDate(value)) return invalid("reminder_date");
    return value;
}
function metadata(fields: ReminderFields) {
    if (Array.from(fields.notes).length > 2000 || fields.notes.includes("\0"))
        return invalid("reminder_notes");
    return { title: boundedName(fields.title, 160, "reminder_title"), notes: fields.notes || null };
}
export function reminderSelection(
    fields: ReminderFields,
    rules: ReminderRule[],
): ReminderRecalculate["rule"] | null {
    if (!fields.ruleKey) return null;
    const rule = rules.find((item) => reminderRuleKey(item) === fields.ruleKey);
    if (!rule) return invalid("reminder_rule_version_unknown");
    const kind = rule.id === "certificate.expiry" ? "certificate" : "annual";
    if (fields.kind !== kind) return invalid("reminder_rule_kind");
    let year: number | null = null;
    if (rule.required.includes("filing_year") && fields.filingYear) {
        if (!/^[0-9]{1,4}$/u.test(fields.filingYear) || Number(fields.filingYear) < 1)
            return invalid("reminder_filing_year");
        year = Number(fields.filingYear);
    }
    return {
        rule_id: rule.id,
        rule_version: rule.version,
        filing_year: year,
        period_end: rule.required.includes("period_end") ? calendar(fields.periodEnd) : null,
        expiry_date: rule.required.includes("expiry_date") ? calendar(fields.expiryDate) : null,
        applicability_confirmed:
            fields.applicability === "yes" ? true : fields.applicability === "no" ? false : null,
    };
}
export function createReminderBody(
    id: string,
    fields: ReminderFields,
    rules: ReminderRule[],
): ReminderCreate {
    const manual = calendar(fields.manualDate);
    if (!manual && fields.manualReason) return invalid("reminder_manual_reason");
    return {
        id,
        event_kind: fields.kind,
        ...metadata(fields),
        rule: reminderSelection(fields, rules),
        manual_due_date: manual,
        manual_reason: manual
            ? boundedName(fields.manualReason, 500, "reminder_manual_reason")
            : null,
    };
}
export function editReminderBody(version: number, fields: ReminderFields): ReminderEdit {
    return { expected_version: version, ...metadata(fields) };
}
export function recalculateReminderBody(
    version: number,
    fields: ReminderFields,
    rules: ReminderRule[],
): ReminderRecalculate {
    const rule = reminderSelection(fields, rules);
    if (!rule) return invalid("reminder_rule_required");
    return { expected_version: version, rule };
}
export function manualReminderBody(
    version: number,
    fields: ReminderFields,
    clear = false,
): ReminderManual {
    const manual = clear ? null : calendar(fields.manualDate);
    if (!clear && !manual) return invalid("reminder_date");
    return {
        expected_version: version,
        manual_due_date: manual,
        reason: boundedName(fields.manualReason, 500, "reminder_manual_reason"),
    };
}
