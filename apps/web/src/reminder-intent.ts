import type { Reminder, ReminderAction } from "./reminder-api";
import { masterName } from "./pending-business";
import { validDraftDate } from "./draft-fields";

export type FrozenReminder = Readonly<{
    ownerId: string;
    ledgerId: string;
    id: string;
    action: ReminderAction;
    bodyJson: string;
}>;
const object = (v: unknown): v is Record<string, unknown> =>
    !!v && typeof v === "object" && !Array.isArray(v);
const text = (v: unknown): v is string => typeof v === "string";
const nullable = (v: unknown) => v === null || text(v);
const integer = (v: unknown, min = 1, max = 2147483647) =>
    typeof v === "number" && Number.isInteger(v) && v >= min && v <= max;
const calendar = (v: unknown) => text(v) && validDraftDate(v);
const dateOrNull = (v: unknown) => v === null || calendar(v);
const strings = (v: unknown) => Array.isArray(v) && v.every(text);
const statuses = ["calculated", "missing_parameters", "needs_verification", "not_applicable"];

export function scopedReminder(value: unknown, plan: FrozenReminder): value is Reminder {
    if (
        !object(value) ||
        value.id !== plan.id ||
        value.ledger_id !== plan.ledgerId ||
        value.last_actor_id !== plan.ownerId ||
        !integer(value.version) ||
        !["annual", "tax", "certificate"].includes(value.event_kind as string) ||
        !text(value.title) ||
        !nullable(value.notes) ||
        !nullable(value.manual_reason) ||
        !nullable(value.last_reason) ||
        typeof value.completed !== "boolean" ||
        typeof value.archived !== "boolean" ||
        !statuses.includes(value.evaluation_status as string) ||
        ![
            "create",
            "edit",
            "recalculate",
            "set_manual",
            "clear_manual",
            "complete",
            "reopen",
            "archive",
            "restore",
        ].includes(value.last_action as string) ||
        ![value.calculated_date, value.manual_due_date, value.effective_date].every(dateOrNull) ||
        ![value.created_at, value.updated_at].every(
            (v) => text(v) && Number.isFinite(Date.parse(v)),
        )
    )
        return false;
    if ((value.manual_due_date === null) !== (value.manual_reason === null)) return false;
    const expected =
        value.manual_due_date ??
        (value.evaluation_status === "calculated" ? value.calculated_date : null);
    if (
        value.effective_date !== expected ||
        (value.evaluation_status === "calculated" && value.calculated_date === null)
    )
        return false;
    if (value.evaluation === null)
        return value.evaluation_status === "missing_parameters" && value.calculated_date === null;
    const evaluation = value.evaluation;
    if (
        !object(evaluation) ||
        evaluation.status !== value.evaluation_status ||
        evaluation.calculated_date !== value.calculated_date ||
        !object(evaluation.inputs) ||
        !object(evaluation.rule) ||
        !strings(evaluation.reasons) ||
        !strings(evaluation.missing)
    )
        return false;
    const rule = evaluation.rule;
    return (
        text(rule.id) &&
        text(rule.version) &&
        calendar(rule.checked_on) &&
        strings(rule.sources) &&
        strings(rule.required)
    );
}

function selectedRule(value: Reminder, body: Record<string, unknown>): boolean {
    if (body.rule == null) return value.evaluation === null;
    if (
        !object(body.rule) ||
        !object(value.evaluation) ||
        !object(value.evaluation.rule) ||
        !object(value.evaluation.inputs)
    )
        return false;
    const selected = body.rule;
    const inputs = value.evaluation.inputs;
    return (
        value.evaluation.rule.id === selected.rule_id &&
        value.evaluation.rule.version === selected.rule_version &&
        ["filing_year", "period_end", "expiry_date", "applicability_confirmed"].every(
            (key) => inputs[key] === (selected[key] ?? null),
        )
    );
}

/** Only a direct response may acknowledge a changed version; unknown updates remain conflicts. */
export function matchesReminder(value: Reminder, plan: FrozenReminder): boolean {
    const body = JSON.parse(plan.bodyJson) as Record<string, unknown>;
    if (value.version !== (plan.action === "create" ? 1 : (body.expected_version as number) + 1))
        return false;
    if (plan.action === "transition") {
        if (value.last_action !== body.action) return false;
        return body.action === "complete"
            ? value.completed
            : body.action === "reopen"
              ? !value.completed
              : body.action === "archive"
                ? value.archived
                : body.action === "restore" && !value.archived;
    }
    if (value.archived) return false;
    if (plan.action === "manual")
        return (
            value.manual_due_date === body.manual_due_date &&
            value.last_action === (body.manual_due_date === null ? "clear_manual" : "set_manual") &&
            text(body.reason) &&
            value.last_reason === masterName(body.reason) &&
            value.manual_reason === (body.manual_due_date === null ? null : masterName(body.reason))
        );
    if (plan.action === "recalculate")
        return value.last_action === "recalculate" && selectedRule(value, body);
    if (
        !text(body.title) ||
        value.title !== masterName(body.title) ||
        value.notes !== (body.notes ?? null)
    )
        return false;
    if (plan.action === "save") return value.last_action === "edit";
    return (
        value.last_action === "create" &&
        !value.completed &&
        value.event_kind === body.event_kind &&
        value.manual_due_date === (body.manual_due_date ?? null) &&
        value.manual_reason ===
            (text(body.manual_reason) ? masterName(body.manual_reason) : null) &&
        selectedRule(value, body)
    );
}
