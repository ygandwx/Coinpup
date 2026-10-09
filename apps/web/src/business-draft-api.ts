import { readJson, writeJson } from "./api";
import type { components } from "./generated/openapi";

export type BusinessDraft = components["schemas"]["BusinessDraftResponse"];
export type BusinessDraftSummary = components["schemas"]["BusinessDraftSummary"];
export type BusinessDraftCreate = components["schemas"]["BusinessDraftCreate"];
export type BusinessDraftUpdate = components["schemas"]["BusinessDraftUpdate"];
export type BusinessDraftArchive = components["schemas"]["BusinessDraftArchive"];
export type BusinessDraftBody = BusinessDraftCreate | BusinessDraftUpdate | BusinessDraftArchive;
export type DraftAction = "create" | "save" | "archive";
const base = (ledger: string) => `/api/v1/ledgers/${encodeURIComponent(ledger)}/business-documents`;
export const listBusinessDrafts = (
    ledger: string,
    offset = 0,
    signal?: AbortSignal,
): Promise<BusinessDraftSummary[]> =>
    readJson(`${base(ledger)}?limit=25&offset=${offset}&include_archived=true`, signal);
export const getBusinessDraft = (
    ledger: string,
    id: string,
    signal?: AbortSignal,
): Promise<BusinessDraft> => readJson(`${base(ledger)}/${encodeURIComponent(id)}`, signal);
export const saveBusinessDraft = (
    csrf: string,
    ledger: string,
    id: string,
    action: DraftAction,
    body: BusinessDraftBody,
): Promise<BusinessDraft> =>
    writeJson(
        base(ledger) +
            (action === "create" ? "" : `/${encodeURIComponent(id)}`) +
            (action === "archive" ? "/archive" : ""),
        csrf,
        body,
        { create: "POST", save: "PUT", archive: "PATCH" }[action] as "POST" | "PUT" | "PATCH",
    );
