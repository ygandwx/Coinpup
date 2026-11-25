import { readJson, writeJson } from "./api";
import type { components } from "./generated/openapi";

export type InboxDelivery = components["schemas"]["ReminderInboxView"];

const path = (ledger: string) => `/api/v1/ledgers/${encodeURIComponent(ledger)}/reminders/inbox`;

export const listInbox = (ledger: string, unread: boolean, offset: number, signal: AbortSignal) =>
    readJson<InboxDelivery[]>(
        `${path(ledger)}?unread_only=${unread}&limit=25&offset=${offset}`,
        signal,
    );

export const markInboxRead = (csrf: string, ledger: string, id: string, signal: AbortSignal) =>
    writeJson<InboxDelivery>(
        `${path(ledger)}/${encodeURIComponent(id)}/read`,
        csrf,
        undefined,
        "PUT",
        undefined,
        signal,
    );
