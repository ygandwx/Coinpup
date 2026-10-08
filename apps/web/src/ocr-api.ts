import { ApiError, readJson, writeJson } from "./api";
import type { components } from "./generated/openapi";

export type OcrJob = components["schemas"]["JobView"];
export type OcrDraft = components["schemas"]["DraftSummary"];
export type DraftDetail = components["schemas"]["DraftDetail"];
export type ReviewView = components["schemas"]["ReviewView"];
export type ReviewUpdate = components["schemas"]["DraftReviewUpdate"];
export type ConfirmationCreate = components["schemas"]["ConfirmationCreate"];
export type ConfirmationReceipt = components["schemas"]["ConfirmationResponse"];
export type DuplicateConfirmation = components["schemas"]["DuplicateConfirmation"];
type Page = { limit?: number; offset?: number };
const root = (ledger: string) => `/api/v1/ledgers/${encodeURIComponent(ledger)}`;
const draft = (ledger: string, id: string) =>
    `${root(ledger)}/ocr-drafts/${encodeURIComponent(id)}`;
function query(options: Page & Record<string, unknown>): string {
    const { limit = 100, offset = 0, ...filters } = options;
    if (
        !Number.isInteger(limit) ||
        limit < 1 ||
        limit > 200 ||
        !Number.isInteger(offset) ||
        offset < 0 ||
        offset > 100000
    )
        throw new ApiError("server", null, "invalid_pagination");
    const result = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    for (const [key, value] of Object.entries(filters))
        if (value !== undefined) result.set(key, String(value));
    return result.toString();
}
export const listOcrJobs = (
    ledger: string,
    options: Page & { intent_id?: string; state?: OcrJob["state"] } = {},
    signal?: AbortSignal,
): Promise<OcrJob[]> => readJson(`${root(ledger)}/ocr-jobs?${query(options)}`, signal);
export const getOcrJob = (ledger: string, id: string, signal?: AbortSignal): Promise<OcrJob> =>
    readJson(`${root(ledger)}/ocr-jobs/${encodeURIComponent(id)}`, signal);
export const startOcrJob = (
    csrf: string,
    ledger: string,
    body: components["schemas"]["JobCreate"],
): Promise<OcrJob> => writeJson(`${root(ledger)}/ocr-jobs`, csrf, body);
export const retryOcrJob = (
    csrf: string,
    ledger: string,
    id: string,
    expected_version: number,
): Promise<OcrJob> =>
    writeJson(`${root(ledger)}/ocr-jobs/${encodeURIComponent(id)}/retries`, csrf, {
        expected_version,
    });
export const listOcrDrafts = (
    ledger: string,
    options: Page & { job_id?: string; status?: OcrDraft["status"] } = {},
    signal?: AbortSignal,
): Promise<OcrDraft[]> => readJson(`${root(ledger)}/ocr-drafts?${query(options)}`, signal);
export const getOcrDraft = (
    ledger: string,
    id: string,
    signal?: AbortSignal,
): Promise<DraftDetail> => readJson(draft(ledger, id), signal);
export const getDraftReview = (
    ledger: string,
    id: string,
    signal?: AbortSignal,
): Promise<ReviewView> => readJson(`${draft(ledger, id)}/review`, signal);
export const saveDraftReview = (
    csrf: string,
    ledger: string,
    id: string,
    body: ReviewUpdate,
): Promise<ReviewView> => writeJson(`${draft(ledger, id)}/review`, csrf, body, "PUT");
export const getConfirmation = (
    ledger: string,
    id: string,
    signal?: AbortSignal,
): Promise<ConfirmationReceipt> => readJson(`${draft(ledger, id)}/confirmation`, signal);
export const listDraftDuplicates = (
    ledger: string,
    id: string,
    signal?: AbortSignal,
): Promise<DuplicateConfirmation[]> => readJson(`${draft(ledger, id)}/duplicates`, signal);
export const confirmDraft = (
    csrf: string,
    ledger: string,
    id: string,
    body: ConfirmationCreate,
): Promise<ConfirmationReceipt> => writeJson(`${draft(ledger, id)}/confirmations`, csrf, body);
