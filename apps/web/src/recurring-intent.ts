import type { RecurringAction, RecurringRule } from "./recurring-api";
import { masterName } from "./pending-business";
import { validDraftDate } from "./draft-fields";

export type FrozenRecurring = Readonly<{
    ownerId: string;
    ledgerId: string;
    id: string;
    action: RecurringAction;
    bodyJson: string;
}>;
const object = (v: unknown): v is Record<string, unknown> =>
    !!v && typeof v === "object" && !Array.isArray(v);
const text = (v: unknown): v is string => typeof v === "string";
const nullable = (v: unknown) => v === null || text(v);
const integer = (v: unknown, min = 1, max = 2147483647) =>
    typeof v === "number" && Number.isInteger(v) && v >= min && v <= max;
const calendarDate = (v: unknown) => text(v) && validDraftDate(v);

export function scopedRecurring(value: unknown, plan: FrozenRecurring): value is RecurringRule {
    if (
        !object(value) ||
        value.id !== plan.id ||
        value.ledger_id !== plan.ledgerId ||
        !integer(value.version) ||
        typeof value.archived !== "boolean" ||
        !text(value.name) ||
        !text(value.timezone_name) ||
        !calendarDate(value.anchor_date) ||
        !["day", "week", "month", "year"].includes(value.frequency as string) ||
        !integer(value.interval_count, 1, 120) ||
        !integer(value.next_index, 0) ||
        !(value.next_scheduled_date === null || calendarDate(value.next_scheduled_date)) ||
        !text(value.source_document_id) ||
        !integer(value.source_version) ||
        ![value.created_at, value.updated_at].every(
            (v) => text(v) && Number.isFinite(Date.parse(v)),
        ) ||
        !object(value.template_input)
    )
        return false;
    const template = value.template_input;
    if (
        template.id !== value.source_document_id ||
        template.document_kind !== "invoice" ||
        !text(template.party_id) ||
        !text(template.asset_id) ||
        !calendarDate(template.issue_date) ||
        !(template.due_date === null || calendarDate(template.due_date)) ||
        !nullable(template.notes) ||
        !Array.isArray(template.lines) ||
        template.lines.length > 200
    )
        return false;
    const ids = new Set<string>();
    for (const line of template.lines) {
        if (
            !object(line) ||
            !text(line.id) ||
            ids.has(line.id) ||
            ![
                "description",
                "quantity",
                "unit_price",
                "discount_amount",
                "tax_rate_percent",
                "category_id",
            ].every((key) => text(line[key])) ||
            !nullable(line.project_id) ||
            !calendarDate(line.recognition_date)
        )
            return false;
        ids.add(line.id);
    }
    return true;
}

/** A current version is evidence of creation only before any edit or generation. */
export function matchesRecurring(value: RecurringRule, plan: FrozenRecurring): boolean {
    const body = JSON.parse(plan.bodyJson) as Record<string, unknown>;
    if (value.version !== (plan.action === "create" ? 1 : (body.expected_version as number) + 1))
        return false;
    if (plan.action === "archive") return value.archived === body.archived;
    if (value.archived || !text(body.name) || value.name !== masterName(body.name)) return false;
    if (plan.action === "create")
        return (
            value.next_index === 0 &&
            [
                "timezone_name",
                "anchor_date",
                "frequency",
                "source_document_id",
                "source_version",
            ].every((key) => value[key as keyof RecurringRule] === body[key]) &&
            value.interval_count === (body.interval_count ?? 1)
        );
    return (
        body.source_document_id == null ||
        (value.source_document_id === body.source_document_id &&
            value.source_version === body.source_version)
    );
}
