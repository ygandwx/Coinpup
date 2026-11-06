import { readJson, writeJson } from "./api";
import type { components } from "./generated/openapi";

export type CreditCreate = components["schemas"]["BusinessCreditCreate"];
export type CreditReceipt = components["schemas"]["BusinessCreditResponse"];
export type CreditCapacity = components["schemas"]["CreditCapacity"];
export type CreditSummary = components["schemas"]["CreditSummary"];
export type CreditDetail = components["schemas"]["CreditDetail"];
export type CreditCorrection = components["schemas"]["CreditCorrectionCreate"];
export type CreditCancellation = components["schemas"]["CreditCancellationCreate"];
export type CreditCurrent = components["schemas"]["CreditCurrent"];
export type CreditRevisionReceipt = components["schemas"]["CreditRevisionResponse"];
export type CreditHistoryEntry = components["schemas"]["CreditHistoryEntry"];
const base = (ledgerId: string) => `/api/v1/ledgers/${encodeURIComponent(ledgerId)}`;

export const createCredit = (
    csrf: string,
    ledgerId: string,
    body: CreditCreate,
    key: string,
): Promise<CreditReceipt> =>
    writeJson(`${base(ledgerId)}/business-credits`, csrf, body, "POST", key);

export const getCreditCapacity = (
    ledgerId: string,
    documentId: string,
    offset = 0,
    signal?: AbortSignal,
): Promise<CreditCapacity> =>
    readJson(
        `${base(ledgerId)}/business-documents/${encodeURIComponent(documentId)}/credit-capacity?limit=200&offset=${offset}`,
        signal,
    );

export const getCredit = (
    ledgerId: string,
    creditId: string,
    signal?: AbortSignal,
): Promise<CreditDetail> =>
    readJson(`${base(ledgerId)}/business-credits/${encodeURIComponent(creditId)}`, signal);

export const listCredits = (
    ledgerId: string,
    documentId?: string,
    offset = 0,
    signal?: AbortSignal,
): Promise<CreditSummary[]> =>
    readJson(
        `${base(ledgerId)}/business-credits?limit=25&offset=${offset}` +
            (documentId === undefined ? "" : `&document_id=${encodeURIComponent(documentId)}`),
        signal,
    );

export const correctCredit = (
    csrf: string,
    ledgerId: string,
    creditId: string,
    body: CreditCorrection,
    key: string,
): Promise<CreditRevisionReceipt> =>
    writeJson(
        `${base(ledgerId)}/business-credits/${encodeURIComponent(creditId)}/corrections`,
        csrf,
        body,
        "POST",
        key,
    );

export const cancelCredit = (
    csrf: string,
    ledgerId: string,
    creditId: string,
    body: CreditCancellation,
    key: string,
): Promise<CreditRevisionReceipt> =>
    writeJson(
        `${base(ledgerId)}/business-credits/${encodeURIComponent(creditId)}/cancellations`,
        csrf,
        body,
        "POST",
        key,
    );

export const getCreditCurrent = (
    ledgerId: string,
    creditId: string,
    signal?: AbortSignal,
): Promise<CreditCurrent> =>
    readJson(`${base(ledgerId)}/business-credits/${encodeURIComponent(creditId)}/current`, signal);

export const listCreditHistory = (
    ledgerId: string,
    creditId: string,
    offset = 0,
    signal?: AbortSignal,
): Promise<CreditHistoryEntry[]> =>
    readJson(
        `${base(ledgerId)}/business-credits/${encodeURIComponent(creditId)}/history?limit=25&offset=${offset}`,
        signal,
    );

export const getCreditRevision = (
    ledgerId: string,
    creditId: string,
    revisionId: string,
    signal?: AbortSignal,
): Promise<CreditRevisionReceipt> =>
    readJson(
        `${base(ledgerId)}/business-credits/${encodeURIComponent(creditId)}/revisions/${encodeURIComponent(revisionId)}`,
        signal,
    );
