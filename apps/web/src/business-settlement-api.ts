import { readJson, writeJson } from "./api";
import type { components } from "./generated/openapi";

export type BusinessPaymentCreate = components["schemas"]["BusinessPaymentCreate"];
export type BusinessAllocationCreate = components["schemas"]["BusinessAllocationCreate"];
export type InitialAllocationCreate = components["schemas"]["InitialAllocationCreate"];
export type BusinessPaymentReceipt = components["schemas"]["BusinessPaymentResponse"];
export type BusinessAllocationReceipt = components["schemas"]["BusinessAllocationResponse"];
export type BusinessPaymentSummary = components["schemas"]["BusinessPaymentSummary"];
export type BusinessPaymentDetail = components["schemas"]["BusinessPaymentDetail"];
export type BusinessAllocationSummary = components["schemas"]["BusinessAllocationSummary"];
export type BusinessAllocationDetail = components["schemas"]["BusinessAllocationDetail"];
export type DocumentSettlementDetail = components["schemas"]["DocumentSettlementDetail"];
export type PaymentEvidenceTarget = components["schemas"]["PaymentEvidenceTarget"];
const base = (ledgerId: string) => `/api/v1/ledgers/${encodeURIComponent(ledgerId)}`;

export const createBusinessPayment = (
    csrf: string,
    ledgerId: string,
    body: BusinessPaymentCreate,
    key: string,
): Promise<BusinessPaymentReceipt> =>
    writeJson(`${base(ledgerId)}/business-payments`, csrf, body, "POST", key);

export const createBusinessAllocation = (
    csrf: string,
    ledgerId: string,
    body: BusinessAllocationCreate,
    key: string,
): Promise<BusinessAllocationReceipt> =>
    writeJson(`${base(ledgerId)}/business-allocations`, csrf, body, "POST", key);

export const listBusinessPayments = (
    ledgerId: string,
    offset = 0,
    signal?: AbortSignal,
): Promise<BusinessPaymentSummary[]> =>
    readJson(`${base(ledgerId)}/business-payments?limit=25&offset=${offset}`, signal);

export const getBusinessPayment = (
    ledgerId: string,
    paymentId: string,
    signal?: AbortSignal,
): Promise<BusinessPaymentDetail> =>
    readJson(`${base(ledgerId)}/business-payments/${encodeURIComponent(paymentId)}`, signal);

export const listBusinessAllocations = (
    ledgerId: string,
    paymentId?: string,
    offset = 0,
    signal?: AbortSignal,
): Promise<BusinessAllocationSummary[]> =>
    readJson(
        `${base(ledgerId)}/business-allocations?limit=25&offset=${offset}` +
            (paymentId === undefined ? "" : `&payment_id=${encodeURIComponent(paymentId)}`),
        signal,
    );

export const getBusinessAllocation = (
    ledgerId: string,
    allocationId: string,
    signal?: AbortSignal,
): Promise<BusinessAllocationDetail> =>
    readJson(`${base(ledgerId)}/business-allocations/${encodeURIComponent(allocationId)}`, signal);

export const getDocumentSettlement = (
    ledgerId: string,
    documentId: string,
    signal?: AbortSignal,
): Promise<DocumentSettlementDetail> =>
    readJson(
        `${base(ledgerId)}/business-documents/${encodeURIComponent(documentId)}/settlement`,
        signal,
    );

export const getPaymentEvidenceTarget = (
    ledgerId: string,
    paymentId: string,
    signal?: AbortSignal,
): Promise<PaymentEvidenceTarget> =>
    readJson(
        `${base(ledgerId)}/business-payments/${encodeURIComponent(paymentId)}/evidence-target`,
        signal,
    );
