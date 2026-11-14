import { readJson, writeJson } from "./api";
import type { components } from "./generated/openapi";

export type RefundCreate = components["schemas"]["BusinessRefundCreate"];
export type RefundReceipt = components["schemas"]["BusinessRefundResponse"];
export type RefundCapacity = components["schemas"]["RefundCapacity"];
export type RefundSummary = components["schemas"]["RefundSummary"];
export type RefundDetail = components["schemas"]["RefundDetail"];
export type RefundCorrection = components["schemas"]["RefundCorrectionCreate"];
export type RefundCancellation = components["schemas"]["RefundCancellationCreate"];
export type RefundRevisionReceipt = components["schemas"]["RefundRevisionResponse"];
export type RefundFact = components["schemas"]["RefundRevisionFact"];
export type RefundCurrent = components["schemas"]["RefundCurrent"];
export type RefundHistoryEntry = components["schemas"]["RefundHistoryEntry"];
const base = (ledgerId: string) => `/api/v1/ledgers/${encodeURIComponent(ledgerId)}`;

export const createRefund = (
    csrf: string,
    ledgerId: string,
    body: RefundCreate,
    key: string,
): Promise<RefundReceipt> =>
    writeJson(`${base(ledgerId)}/business-refunds`, csrf, body, "POST", key);

export const getRefundCapacity = (
    ledgerId: string,
    paymentId: string,
    signal?: AbortSignal,
): Promise<RefundCapacity> =>
    readJson(
        `${base(ledgerId)}/business-payments/${encodeURIComponent(paymentId)}/refund-capacity`,
        signal,
    );

export const getRefund = (
    ledgerId: string,
    refundId: string,
    signal?: AbortSignal,
): Promise<RefundDetail> =>
    readJson(`${base(ledgerId)}/business-refunds/${encodeURIComponent(refundId)}`, signal);

export const listRefunds = (
    ledgerId: string,
    paymentId?: string,
    offset = 0,
    signal?: AbortSignal,
): Promise<RefundSummary[]> =>
    readJson(
        `${base(ledgerId)}/business-refunds?limit=25&offset=${offset}` +
            (paymentId === undefined ? "" : `&payment_id=${encodeURIComponent(paymentId)}`),
        signal,
    );

export const correctRefund = (
    csrf: string,
    ledgerId: string,
    refundId: string,
    body: RefundCorrection,
    key: string,
): Promise<RefundRevisionReceipt> =>
    writeJson(
        `${base(ledgerId)}/business-refunds/${encodeURIComponent(refundId)}/corrections`,
        csrf,
        body,
        "POST",
        key,
    );

export const cancelRefund = (
    csrf: string,
    ledgerId: string,
    refundId: string,
    body: RefundCancellation,
    key: string,
): Promise<RefundRevisionReceipt> =>
    writeJson(
        `${base(ledgerId)}/business-refunds/${encodeURIComponent(refundId)}/cancellations`,
        csrf,
        body,
        "POST",
        key,
    );

export const getRefundCurrent = (
    ledgerId: string,
    refundId: string,
    signal?: AbortSignal,
): Promise<RefundCurrent> =>
    readJson(`${base(ledgerId)}/business-refunds/${encodeURIComponent(refundId)}/current`, signal);

export const listRefundHistory = (
    ledgerId: string,
    refundId: string,
    offset = 0,
    signal?: AbortSignal,
): Promise<RefundHistoryEntry[]> =>
    readJson(
        `${base(ledgerId)}/business-refunds/${encodeURIComponent(refundId)}/history?limit=25&offset=${offset}`,
        signal,
    );

export const getRefundRevision = (
    ledgerId: string,
    refundId: string,
    revisionId: string,
    signal?: AbortSignal,
): Promise<RefundRevisionReceipt> =>
    readJson(
        `${base(ledgerId)}/business-refunds/${encodeURIComponent(refundId)}/revisions/${encodeURIComponent(revisionId)}`,
        signal,
    );
