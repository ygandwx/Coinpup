import { readJson, writeJson } from "./api";
import type { components } from "./generated/openapi";

export type AllocationReturnCreate = components["schemas"]["BusinessAllocationReturnCreate"];
export type AllocationReturnReceipt = components["schemas"]["BusinessAllocationReturnResponse"];
export type AllocationReturnCapacity = components["schemas"]["AllocationReturnCapacity"];
export type AllocationReturnSummary = components["schemas"]["AllocationReturnSummary"];
export type AllocationReturnDetail = components["schemas"]["AllocationReturnDetail"];
const base = (ledgerId: string) => `/api/v1/ledgers/${encodeURIComponent(ledgerId)}`;

export const createAllocationReturn = (
    csrf: string,
    ledgerId: string,
    body: AllocationReturnCreate,
    key: string,
): Promise<AllocationReturnReceipt> =>
    writeJson(`${base(ledgerId)}/business-allocation-returns`, csrf, body, "POST", key);

export const getAllocationReturnCapacity = (
    ledgerId: string,
    allocationId: string,
    signal?: AbortSignal,
): Promise<AllocationReturnCapacity> =>
    readJson(
        `${base(ledgerId)}/business-allocations/${encodeURIComponent(allocationId)}/return-capacity`,
        signal,
    );

export const getAllocationReturn = (
    ledgerId: string,
    returnId: string,
    signal?: AbortSignal,
): Promise<AllocationReturnDetail> =>
    readJson(
        `${base(ledgerId)}/business-allocation-returns/${encodeURIComponent(returnId)}`,
        signal,
    );

export const listAllocationReturns = (
    ledgerId: string,
    paymentId?: string,
    offset = 0,
    signal?: AbortSignal,
): Promise<AllocationReturnSummary[]> =>
    readJson(
        `${base(ledgerId)}/business-allocation-returns?limit=25&offset=${offset}` +
            (paymentId === undefined ? "" : `&payment_id=${encodeURIComponent(paymentId)}`),
        signal,
    );
