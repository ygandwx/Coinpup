import { ApiError, readJson } from "./api";
import type { components } from "./generated/openapi";
import { record, keys } from "./credit-intent";
import { amountFromMinorUnits, parseAmount } from "./money";
export type ActivityPage = components["schemas"]["ProjectActivityPage"];
export const amountFields = [
    "income",
    "cost",
    "net",
    "closing_receivable",
    "closing_payable",
    "attributed_received",
    "attributed_paid",
] as const;
export const limit = 25;
export type ActivityQuery = {
    ledger: string;
    project: string;
    start: string;
    end: string;
    offset: number;
};
export function checkActivity(value: unknown, q: ActivityQuery): value is ActivityPage {
    if (
        !record(value) ||
        !keys(value, [
            "ledger_id",
            "project_id",
            "start",
            "end",
            "offset",
            "limit",
            "has_more",
            "items",
        ]) ||
        value.ledger_id !== q.ledger ||
        value.project_id !== q.project ||
        value.start !== q.start ||
        value.end !== q.end ||
        value.offset !== q.offset ||
        value.limit !== limit ||
        typeof value.has_more !== "boolean" ||
        !Array.isArray(value.items) ||
        value.items.length > limit ||
        (value.has_more && value.items.length !== limit)
    )
        return false;
    const seen = new Set<string>();
    for (const row of value.items) {
        if (
            !record(row) ||
            !keys(row, ["asset_id", ...amountFields]) ||
            typeof row.asset_id !== "string" ||
            !row.asset_id ||
            row.asset_id.length > 200 ||
            seen.has(row.asset_id) ||
            typeof row.income !== "string"
        )
            return false;
        seen.add(row.asset_id);
        const scale = row.income.split(".")[1]?.length ?? 0;
        try {
            const units = amountFields.map((k) => {
                const s = row[k];
                if (typeof s !== "string") throw new Error();
                const n = parseAmount(s, scale);
                if (amountFromMinorUnits(n, scale) !== s) throw new Error();
                return n;
            });
            if (units[0] - units[1] !== units[2] || units[5] < 0n || units[6] < 0n) return false;
        } catch {
            return false;
        }
    }
    return true;
}
export async function readActivity(q: ActivityQuery, signal: AbortSignal): Promise<ActivityPage> {
    const params = new URLSearchParams({
        start: q.start,
        end: q.end,
        limit: String(limit),
        offset: String(q.offset),
    });
    const result = await readJson<unknown>(
        "/api/v1/ledgers/" +
            encodeURIComponent(q.ledger) +
            "/business-projects/" +
            encodeURIComponent(q.project) +
            "/activity?" +
            params,
        signal,
    );
    if (!checkActivity(result, q)) throw new ApiError("server", null, "invalid_activity");
    return result;
}
