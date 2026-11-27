import { readJson, writeJson } from "./api";
import type { components } from "./generated/openapi";

export type Reminder = components["schemas"]["ReminderResponse"];
export type ReminderRule = components["schemas"]["ReminderRuleResponse"];
export type ReminderRevision = components["schemas"]["ReminderRevisionResponse"];
export type ReminderCreate = components["schemas"]["ReminderCreate"];
export type ReminderEdit = components["schemas"]["ReminderEdit"];
export type ReminderRecalculate = components["schemas"]["ReminderRecalculate"];
export type ReminderManual = components["schemas"]["ReminderManual"];
export type ReminderTransition = components["schemas"]["ReminderTransition"];
export type ReminderBody =
    ReminderCreate | ReminderEdit | ReminderRecalculate | ReminderManual | ReminderTransition;
export type ReminderAction = "create" | "save" | "recalculate" | "manual" | "transition";
const base = (ledger: string) => `/api/v1/ledgers/${encodeURIComponent(ledger)}/reminders`;
export const listReminders = (
    ledger: string,
    offset = 0,
    signal?: AbortSignal,
): Promise<Reminder[]> =>
    readJson(
        `${base(ledger)}?limit=25&offset=${offset}&include_archived=true&include_completed=true`,
        signal,
    );
export const getReminder = (ledger: string, id: string, signal?: AbortSignal): Promise<Reminder> =>
    readJson(`${base(ledger)}/${encodeURIComponent(id)}`, signal);
export const listReminderRules = (ledger: string, signal?: AbortSignal): Promise<ReminderRule[]> =>
    readJson(`${base(ledger)}/rules`, signal);
export const listReminderRevisions = (
    ledger: string,
    id: string,
    offset = 0,
    signal?: AbortSignal,
): Promise<ReminderRevision[]> =>
    readJson(
        `${base(ledger)}/${encodeURIComponent(id)}/revisions?limit=25&offset=${offset}`,
        signal,
    );
export const saveReminder = (
    csrf: string,
    ledger: string,
    id: string,
    action: ReminderAction,
    body: ReminderBody,
): Promise<Reminder> => {
    const suffix = {
        create: "",
        save: "",
        recalculate: "/recalculate",
        manual: "/manual-date",
        transition: "/transition",
    }[action];
    return writeJson(
        base(ledger) + (action === "create" ? "" : `/${encodeURIComponent(id)}`) + suffix,
        csrf,
        body,
        action === "save" || action === "manual" ? "PATCH" : "POST",
    );
};

export async function listUpcomingReminders(
    ledger: string,
    today: string,
    signal: AbortSignal,
): Promise<Reminder[]> {
    const upcoming: Reminder[] = [];
    const limit = 100;
    for (let offset = 0; offset <= 100000; offset += limit) {
        const page = await readJson<Reminder[]>(
            `${base(ledger)}?limit=${limit}&offset=${offset}&include_archived=false&include_completed=false`,
            signal,
        );
        upcoming.push(
            ...page.filter((item) => item.effective_date && item.effective_date >= today),
        );
        if (page.length < limit) {
            return upcoming
                .sort((a, b) => a.effective_date!.localeCompare(b.effective_date!))
                .slice(0, 5);
        }
    }
    throw new Error("Reminder page limit exceeded");
}
