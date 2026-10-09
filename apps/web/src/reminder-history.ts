import type { ReminderRevision } from "./reminder-api";
import { scopedReminder } from "./reminder-intent";

export function reminderHistoryRows(
    value: unknown,
    ownerId: string,
    ledgerId: string,
    eventId: string,
    offset: number,
): value is ReminderRevision[] {
    if (!Array.isArray(value) || value.length > 25 || !Number.isInteger(offset) || offset < 0)
        return false;
    return value.every((row, index) => {
        if (
            !row ||
            typeof row !== "object" ||
            row.ledger_id !== ledgerId ||
            row.event_id !== eventId ||
            row.version !== offset + index + 1 ||
            typeof row.id !== "string" ||
            typeof row.created_at !== "string" ||
            !Number.isFinite(Date.parse(row.created_at)) ||
            !row.snapshot ||
            typeof row.snapshot !== "object" ||
            Array.isArray(row.snapshot) ||
            row.snapshot.version !== row.version
        )
            return false;
        const snapshot = row.snapshot;
        // Database snapshots predate the response-only effective_date field. Derive only from saved dates.
        const effective_date =
            snapshot.manual_due_date ??
            (snapshot.evaluation_status === "calculated" ? snapshot.calculated_date : null);
        return scopedReminder(
            { ...snapshot, effective_date },
            { ownerId, ledgerId, id: eventId, action: "create", bodyJson: "{}" },
        );
    });
}
