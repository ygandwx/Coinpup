import { readJson, writeJson } from "./api";
import type { components } from "./generated/openapi";

export type RecurringRule = components["schemas"]["RecurringRuleResponse"];
export type RecurringInstance = components["schemas"]["RecurringInstanceResponse"];
export type RecurringCreate = components["schemas"]["RecurringRuleCreate"];
export type RecurringUpdate = components["schemas"]["RecurringRuleUpdate"];
export type RecurringArchive = components["schemas"]["RecurringRuleArchive"];
export type RecurringBody = RecurringCreate | RecurringUpdate | RecurringArchive;
export type RecurringAction = "create" | "save" | "archive";
const base = (ledger: string) =>
    `/api/v1/ledgers/${encodeURIComponent(ledger)}/recurring-invoice-rules`;
export const listRecurringRules = (
    ledger: string,
    offset = 0,
    signal?: AbortSignal,
): Promise<RecurringRule[]> =>
    readJson(`${base(ledger)}?limit=25&offset=${offset}&include_archived=true`, signal);
export const getRecurringRule = (
    ledger: string,
    id: string,
    signal?: AbortSignal,
): Promise<RecurringRule> => readJson(`${base(ledger)}/${encodeURIComponent(id)}`, signal);
export const listRecurringInstances = (
    ledger: string,
    id: string,
    offset = 0,
    signal?: AbortSignal,
): Promise<RecurringInstance[]> =>
    readJson(
        `${base(ledger)}/${encodeURIComponent(id)}/instances?limit=25&offset=${offset}`,
        signal,
    );
export const saveRecurringRule = (
    csrf: string,
    ledger: string,
    id: string,
    action: RecurringAction,
    body: RecurringBody,
): Promise<RecurringRule> =>
    writeJson(
        base(ledger) +
            (action === "create" ? "" : `/${encodeURIComponent(id)}`) +
            (action === "archive" ? "/archive" : ""),
        csrf,
        body,
        action === "create" ? "POST" : "PATCH",
    );
