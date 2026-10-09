import type { BusinessDraftSummary } from "./business-draft-api";
import type { RecurringCreate, RecurringRule } from "./recurring-api";
import { validDraftDate } from "./draft-fields";
import { masterName } from "./pending-business";

export type RecurringFields = {
    name: string;
    timezone: string;
    anchor: string;
    frequency: RecurringCreate["frequency"];
    interval: string;
};
export const recurringFields = (date: string, rule?: RecurringRule): RecurringFields => ({
    name: rule?.name ?? "",
    timezone: rule?.timezone_name ?? "",
    anchor: rule?.anchor_date ?? date,
    frequency: rule?.frequency ?? "month",
    interval: String(rule?.interval_count ?? 1),
});
export function validRecurringFields(fields: RecurringFields, existing: boolean): boolean {
    if (!masterName(fields.name) || fields.name.length > 160 || fields.name.includes("\0"))
        return false;
    return (
        existing ||
        (validDraftDate(fields.anchor) &&
            fields.timezone.length > 0 &&
            fields.timezone.length <= 100 &&
            fields.timezone === fields.timezone.trim() &&
            !fields.timezone.includes("\0") &&
            ["day", "week", "month", "year"].includes(fields.frequency) &&
            /^[1-9]\d{0,2}$/.test(fields.interval) &&
            Number(fields.interval) <= 120)
    );
}
export const usableRecurringSource = (
    source: BusinessDraftSummary | null,
): source is BusinessDraftSummary =>
    !!source && source.document_kind === "invoice" && !source.archived && source.state === "draft";
export function recurringCreate(
    id: string,
    fields: RecurringFields,
    source: BusinessDraftSummary,
): RecurringCreate {
    if (!validRecurringFields(fields, false) || !usableRecurringSource(source))
        throw new Error("invalid_recurring_fields");
    return {
        id,
        name: fields.name,
        timezone_name: fields.timezone,
        anchor_date: fields.anchor,
        frequency: fields.frequency,
        interval_count: Number(fields.interval),
        source_document_id: source.id,
        source_version: source.version,
    };
}
