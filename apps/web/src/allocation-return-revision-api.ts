import { readJson, writeJson } from "./api";
import type { components } from "./generated/openapi";

type Wire = components["schemas"];
type ExactPosition<T> = Omit<T, "source_return_no"> & { source_return_no: string };
type MembersWithExactPositions<T extends { members: { source_return_no: number }[] }> = Omit<
    T,
    "members"
> & { members: ExactPosition<T["members"][number]>[] };

export type ReturnCurrent = MembersWithExactPositions<Wire["AllocationReturnCurrent"]>;
export type ReturnRevisionReceipt = MembersWithExactPositions<
    Wire["AllocationReturnRevisionResponse"]
>;
export type ReturnHistoryEntry = Wire["AllocationReturnHistoryEntry"];
export type ReturnCorrection = Wire["AllocationReturnCorrectionCreate"];
export type ReturnCancellation = Wire["AllocationReturnCancellationCreate"];

function groupPath(ledgerId: string, returnId: string) {
    return `/api/v1/ledgers/${encodeURIComponent(ledgerId)}/business-allocation-returns/${encodeURIComponent(returnId)}`;
}

// PostgreSQL reserves BIGINT source positions. Preserve the JSON integer token before the
// JS number loses precision. Small exact values also work in engines without source context.
export function decodeReturnPositions(text: string): unknown {
    return JSON.parse(text, (key: string, value: unknown, context?: { source?: string }) => {
        if (key !== "source_return_no") return value;
        if (typeof value !== "number") throw new Error("Invalid return source position");
        const source = context?.source ?? (Number.isSafeInteger(value) ? String(value) : "");
        if (!/^[1-9][0-9]{0,18}$/u.test(source) || BigInt(source) > 9223372036854775807n)
            throw new Error("Invalid return source position");
        return source;
    });
}

export const currentReturn = (
    ledgerId: string,
    returnId: string,
    signal?: AbortSignal,
): Promise<ReturnCurrent> =>
    readJson(groupPath(ledgerId, returnId) + "/current", signal, decodeReturnPositions);

export const returnHistory = (
    ledgerId: string,
    returnId: string,
    offset = 0,
    signal?: AbortSignal,
): Promise<ReturnHistoryEntry[]> =>
    readJson(groupPath(ledgerId, returnId) + `/history?limit=25&offset=${offset}`, signal);

export const returnRevision = (
    ledgerId: string,
    returnId: string,
    revisionId: string,
    signal?: AbortSignal,
): Promise<ReturnRevisionReceipt> =>
    readJson(
        groupPath(ledgerId, returnId) + "/revisions/" + encodeURIComponent(revisionId),
        signal,
        decodeReturnPositions,
    );

export const correctReturn = (
    csrf: string,
    ledgerId: string,
    returnId: string,
    body: ReturnCorrection,
    key: string,
): Promise<ReturnRevisionReceipt> =>
    writeJson(
        groupPath(ledgerId, returnId) + "/corrections",
        csrf,
        body,
        "POST",
        key,
        undefined,
        decodeReturnPositions,
    );

export const cancelReturn = (
    csrf: string,
    ledgerId: string,
    returnId: string,
    body: ReturnCancellation,
    key: string,
): Promise<ReturnRevisionReceipt> =>
    writeJson(
        groupPath(ledgerId, returnId) + "/cancellations",
        csrf,
        body,
        "POST",
        key,
        undefined,
        decodeReturnPositions,
    );
