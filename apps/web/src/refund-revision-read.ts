import type {
    RefundCurrent,
    RefundFact,
    RefundHistoryEntry,
    RefundRevisionReceipt,
} from "./refund-api";
import type { RefundReview } from "./refund-intent";
import { assetId, keys, record, uuid } from "./refund-intent";
import { stripCommandText } from "./command-text";
import { validDraftDate } from "./draft-fields";
import { parseAmount } from "./money";

type AssetScales = ReadonlyArray<Pick<RefundReview["assets"][number], "asset_id" | "scale">>;
export type RefundScope = Pick<
    RefundCurrent,
    "ledger_id" | "refund_id" | "payment_id" | "operation_id" | "payment_asset_id"
>;
const version = (v: unknown): v is number =>
    Number.isSafeInteger(v) && (v as number) >= 1 && (v as number) <= 2147483647;
const date = (v: unknown): v is string => typeof v === "string" && validDraftDate(v);
const reason = (v: unknown, max: number): v is string =>
    typeof v === "string" &&
    !!v &&
    !v.includes("\0") &&
    v === stripCommandText(v) &&
    [...v].length <= max;

function matchesScope(value: Record<string, unknown>, scope: RefundScope): boolean {
    return (
        (["refund_id", "ledger_id", "payment_id", "operation_id"] as const).every(
            (k) => uuid(scope[k]) && value[k] === scope[k],
        ) && assetId(scope.payment_asset_id)
    );
}

// Stored NUMERIC(38,18) may include trailing zeros beyond an asset's input precision.
export function storedRefundUnits(value: unknown, asset: string, assets: AssetScales): bigint {
    const rows = assets.filter((a) => a.asset_id === asset);
    if (
        rows.length !== 1 ||
        !Number.isInteger(rows[0].scale) ||
        rows[0].scale < 0 ||
        rows[0].scale > 18 ||
        typeof value !== "string"
    )
        throw new Error("Invalid stored refund asset or amount");
    const units = parseAmount(value, 18),
        divisor = 10n ** BigInt(18 - rows[0].scale);
    if (units <= 0n || units % divisor !== 0n) throw new Error("Invalid stored refund precision");
    return units / divisor;
}

export function validRefundFact(
    value: unknown,
    principal: string,
    assets: AssetScales,
): value is RefundFact {
    if (
        !record(value) ||
        !keys(value, [
            "account_id",
            "asset_id",
            "amount",
            "payment_asset_id",
            "payment_amount",
            "source_version",
            "transaction_date",
            "recognition_date",
            "reason",
            "fees",
            "allocation_return_ids",
        ]) ||
        !uuid(value.account_id) ||
        !assetId(value.asset_id) ||
        !assetId(principal) ||
        value.payment_asset_id !== principal ||
        value.source_version !== 1 ||
        !date(value.transaction_date) ||
        value.recognition_date !== value.transaction_date ||
        !reason(value.reason, 2000) ||
        !Array.isArray(value.fees) ||
        value.fees.length > 20 ||
        !Array.isArray(value.allocation_return_ids) ||
        value.allocation_return_ids.length > 200 ||
        !value.allocation_return_ids.every(uuid) ||
        new Set(value.allocation_return_ids).size !== value.allocation_return_ids.length
    )
        return false;
    try {
        const amount = storedRefundUnits(value.amount, value.asset_id, assets),
            paid = storedRefundUnits(value.payment_amount, principal, assets);
        return (
            (value.asset_id !== principal || amount === paid) &&
            value.fees.every(
                (f) =>
                    record(f) &&
                    keys(f, ["account_id", "category_id", "asset_id", "amount"]) &&
                    uuid(f.account_id) &&
                    uuid(f.category_id) &&
                    assetId(f.asset_id) &&
                    storedRefundUnits(f.amount, f.asset_id, assets) > 0n,
            )
        );
    } catch {
        return false;
    }
}

export function validRefundCurrent(
    value: unknown,
    scope: RefundScope,
    partyId: string,
    direction: "receipt" | "payout",
    assets: AssetScales,
): value is RefundCurrent {
    if (
        !record(value) ||
        !keys(value, [
            "refund_id",
            "ledger_id",
            "payment_id",
            "operation_id",
            "party_id",
            "payment_direction",
            "payment_asset_id",
            "version",
            "status",
            "revision_id",
            "revision_reason",
            "journal_id",
            "fact",
        ]) ||
        !matchesScope(value, scope) ||
        !uuid(partyId) ||
        value.party_id !== partyId ||
        value.payment_direction !== direction ||
        !["receipt", "payout"].includes(direction) ||
        value.payment_asset_id !== scope.payment_asset_id ||
        !version(value.version) ||
        (value.version === 1
            ? value.revision_id !== null || value.revision_reason !== null
            : !uuid(value.revision_id) || !reason(value.revision_reason, 1000))
    )
        return false;
    return value.status === "active"
        ? uuid(value.journal_id) && validRefundFact(value.fact, scope.payment_asset_id, assets)
        : value.status === "cancelled" &&
              value.version > 1 &&
              value.journal_id === null &&
              value.fact === null;
}

export function validRefundHistoryPage(
    rows: unknown,
    offset: number,
): rows is RefundHistoryEntry[] {
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

export function validRefundRevisionDetail(
    value: unknown,
    scope: RefundScope,
    entry: Pick<RefundHistoryEntry, "version" | "revision_id" | "action" | "reason">,
    assets: AssetScales,
): value is RefundRevisionReceipt {
    if (
        !record(value) ||
        !keys(value, [
            "kind",
            "refund_id",
            "revision_id",
            "ledger_id",
            "payment_id",
            "operation_id",
            "previous_version",
            "version",
            "action",
            "reason",
            "previous_journal_id",
            "reversal_journal_id",
            "replacement_journal_id",
            "previous",
            "replacement",
        ]) ||
        !matchesScope(value, scope) ||
        value.kind !== "business_refund_revision" ||
        !uuid(entry.revision_id) ||
        value.revision_id !== entry.revision_id ||
        value.version !== entry.version ||
        !version(value.previous_version) ||
        !version(entry.version) ||
        entry.version !== value.previous_version + 1 ||
        value.action !== entry.action ||
        !reason(entry.reason, 1000) ||
        value.reason !== entry.reason ||
        !uuid(value.previous_journal_id) ||
        !uuid(value.reversal_journal_id) ||
        value.previous_journal_id === value.reversal_journal_id ||
        !validRefundFact(value.previous, scope.payment_asset_id, assets)
    )
        return false;
    return entry.action === "cancel"
        ? value.replacement === null && value.replacement_journal_id === null
        : entry.action === "correct" &&
              uuid(value.replacement_journal_id) &&
              value.replacement_journal_id !== value.previous_journal_id &&
              value.replacement_journal_id !== value.reversal_journal_id &&
              validRefundFact(value.replacement, scope.payment_asset_id, assets);
}
