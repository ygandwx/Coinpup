import type { RefundHistoryEntry } from "./refund-api";
import type { RefundReview } from "./refund-intent";
import { keys, record, uuid } from "./refund-intent";
import { stripCommandText } from "./command-text";
import { validDraftDate } from "./draft-fields";
import { parseAmount } from "./money";

export type AssetScales = ReadonlyArray<Pick<RefundReview["assets"][number], "asset_id" | "scale">>;
export const version = (v: unknown): v is number =>
    Number.isSafeInteger(v) && (v as number) >= 1 && (v as number) <= 2147483647;
export const date = (v: unknown): v is string => typeof v === "string" && validDraftDate(v);
export const reason = (v: unknown, max: number): v is string =>
    typeof v === "string" &&
    !!v &&
    !v.includes("\0") &&
    v === stripCommandText(v) &&
    [...v].length <= max;

// Stored NUMERIC(38,18) may include trailing zeros beyond an asset's input precision.
export function storedUnits(value: unknown, asset: string, assets: AssetScales): bigint {
    const rows = assets.filter((a) => a.asset_id === asset);
    if (
        rows.length !== 1 ||
        !Number.isInteger(rows[0].scale) ||
        rows[0].scale < 0 ||
        rows[0].scale > 18 ||
        typeof value !== "string"
    )
        throw new Error("Invalid stored asset or amount");
    const units = parseAmount(value, 18),
        divisor = 10n ** BigInt(18 - rows[0].scale);
    if (units <= 0n || units % divisor !== 0n) throw new Error("Invalid stored precision");
    return units / divisor;
}

export function validHistoryPage(rows: unknown, offset: number): rows is RefundHistoryEntry[] {
    return (
        Number.isSafeInteger(offset) &&
        offset >= 0 &&
        offset <= 100000 &&
        Array.isArray(rows) &&
        rows.length <= 25 &&
        (offset !== 0 || rows.length > 0) &&
        new Set(rows.filter(record).map((row) => row.revision_id)).size === rows.length &&
        rows.every(
            (row, i) =>
                record(row) &&
                keys(row, ["version", "revision_id", "action", "reason", "created_at"]) &&
                row.version === offset + i + 1 &&
                version(row.version) &&
                reason(row.reason, row.version === 1 ? 2000 : 1000) &&
                typeof row.created_at === "string" &&
                /^\d{4}-\d{2}-\d{2}T/u.test(row.created_at) &&
                date(row.created_at.slice(0, 10)) &&
                Number.isFinite(Date.parse(row.created_at)) &&
                (row.version === 1
                    ? row.action === "create" && row.revision_id === null
                    : uuid(row.revision_id) &&
                      (row.action === "correct" ||
                          (row.action === "cancel" && i === rows.length - 1))),
        )
    );
}
