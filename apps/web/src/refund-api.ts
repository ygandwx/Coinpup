import { readJson, writeJson } from "./api";
import type { components } from "./generated/openapi";

export type RefundCreate = components["schemas"]["BusinessRefundCreate"];
export type RefundReceipt = components["schemas"]["BusinessRefundResponse"];
export type RefundCapacity = components["schemas"]["RefundCapacity"];
export type RefundSummary = components["schemas"]["RefundSummary"];
export type RefundDetail = components["schemas"]["RefundDetail"];
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
