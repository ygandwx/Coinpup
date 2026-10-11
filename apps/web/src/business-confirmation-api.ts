import { readJson, writeJson } from "./api";
import type { components } from "./generated/openapi";

export type BusinessConfirmationCreate = components["schemas"]["BusinessConfirmationCreate"];
export type BusinessConfirmationReceipt = components["schemas"]["BusinessConfirmationResponse"];
export type BusinessConfirmationDetail = components["schemas"]["BusinessConfirmationDetail"];
export type BusinessConfirmedSummary = components["schemas"]["BusinessConfirmedSummary"];
const base = (ledgerId: string) => `/api/v1/ledgers/${encodeURIComponent(ledgerId)}`;
const document = (ledgerId: string, documentId: string) =>
    `${base(ledgerId)}/business-documents/${encodeURIComponent(documentId)}`;
export const confirmBusinessDocument = (
    csrf: string,
    ledgerId: string,
    documentId: string,
    body: BusinessConfirmationCreate,
    key: string,
): Promise<BusinessConfirmationReceipt> =>
    writeJson(`${document(ledgerId, documentId)}/confirm`, csrf, body, "POST", key);
export const getBusinessConfirmation = (
    ledgerId: string,
    documentId: string,
    signal?: AbortSignal,
): Promise<BusinessConfirmationDetail> =>
    readJson(`${document(ledgerId, documentId)}/confirmation`, signal);
export const listBusinessConfirmations = (
    ledgerId: string,
    offset = 0,
    signal?: AbortSignal,
): Promise<BusinessConfirmedSummary[]> =>
    readJson(`${base(ledgerId)}/business-confirmations?limit=25&offset=${offset}`, signal);
