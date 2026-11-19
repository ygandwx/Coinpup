import { readJson, writeJson } from "./api";
import type { components } from "./generated/openapi";

export type DocumentCurrent = components["schemas"]["DocumentCurrent"];
export type DocumentHistoryEntry = components["schemas"]["DocumentHistoryEntry"];
export type DocumentRevisionReceipt = components["schemas"]["DocumentRevisionResponse"];
export type DocumentCorrection = components["schemas"]["ConfirmedDocumentCorrection"];
export type DocumentCancellation = components["schemas"]["ConfirmedDocumentCancellation"];
export type DocumentReplacement = components["schemas"]["ConfirmedDocumentReplacement"];

const target = (ledgerId: string, documentId: string) =>
    `/api/v1/ledgers/${encodeURIComponent(ledgerId)}/business-documents/${encodeURIComponent(documentId)}`;

export const getDocumentCurrent = (
    ledgerId: string,
    documentId: string,
    signal?: AbortSignal,
): Promise<DocumentCurrent> => readJson(`${target(ledgerId, documentId)}/current`, signal);

export const listDocumentHistory = (
    ledgerId: string,
    documentId: string,
    offset = 0,
    signal?: AbortSignal,
): Promise<DocumentHistoryEntry[]> =>
    readJson(`${target(ledgerId, documentId)}/history?limit=25&offset=${offset}`, signal);

export const getDocumentRevision = (
    ledgerId: string,
    documentId: string,
    revisionId: string,
    signal?: AbortSignal,
): Promise<DocumentRevisionReceipt> =>
    readJson(`${target(ledgerId, documentId)}/revisions/${encodeURIComponent(revisionId)}`, signal);

export const correctDocument = (
    csrf: string,
    ledgerId: string,
    documentId: string,
    body: DocumentCorrection,
    key: string,
): Promise<DocumentRevisionReceipt> =>
    writeJson(`${target(ledgerId, documentId)}/corrections`, csrf, body, "POST", key);

export const cancelDocument = (
    csrf: string,
    ledgerId: string,
    documentId: string,
    body: DocumentCancellation,
    key: string,
): Promise<DocumentRevisionReceipt> =>
    writeJson(`${target(ledgerId, documentId)}/cancellations`, csrf, body, "POST", key);
