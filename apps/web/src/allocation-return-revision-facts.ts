import type {
    ReturnCurrent,
    ReturnRevisionReceipt,
    ReturnHistoryEntry,
} from "./allocation-return-revision-api";
import { assetId, keys, record, uuid } from "./refund-intent";
import { date, reason, storedUnits, version } from "./settlement-revision-facts";
import type { AssetScales } from "./settlement-revision-facts";

export type ReturnScope = Pick<
    ReturnCurrent,
    "ledger_id" | "return_id" | "payment_id" | "payment_asset_id"
>;
type Fields = Record<string, unknown>;
const common = [
    "source_allocation_line_id",
    "source_operation_id",
    "source_version",
    "source_return_no",
    "operation_id",
    "operation_version",
    "document_id",
    "document_line_id",
    "document_asset_id",
];
const distinct = (ids: readonly unknown[]) => new Set(ids).size === ids.length;
const position = (n: unknown): n is number =>
    typeof n === "number" && Number.isInteger(n) && n >= 1 && n <= 200;
const consecutive = (ns: number[]) => ns.every((n, i) => n === i + 1);
function scopeMatches(row: Fields, scope: ReturnScope) {
    return (
        (["ledger_id", "return_id", "payment_id"] as const).every(
            (k) => uuid(scope[k]) && row[k] === scope[k],
        ) &&
        assetId(scope.payment_asset_id) &&
        row.payment_asset_id === scope.payment_asset_id
    );
}
function memberIdentity(m: Fields, groupVersion: number) {
    return (
        [
            "source_allocation_line_id",
            "source_operation_id",
            "operation_id",
            "document_id",
            "document_line_id",
        ].every((k) => uuid(m[k])) &&
        m.source_version === 1 &&
        version(m.operation_version) &&
        m.operation_version <= groupVersion &&
        typeof m.source_return_no === "string" &&
        /^[1-9][0-9]{0,18}$/u.test(m.source_return_no) &&
        BigInt(m.source_return_no) <= 9223372036854775807n &&
        assetId(m.document_asset_id)
    );
}
function pair(row: Fields, paymentAsset: string, docAsset: string, assets: AssetScales) {
    const p = storedUnits(row.payment_amount, paymentAsset, assets),
        d = storedUnits(row.document_amount, docAsset, assets);
    return paymentAsset !== docAsset || p === d;
}
function businessFact(row: unknown) {
    return (
        record(row) &&
        keys(row, ["return_date", "reason"]) &&
        date(row.return_date) &&
        reason(row.reason, 2000)
    );
}
function identities(members: Fields[]) {
    const operations = new Set(members.map((m) => m.operation_id));
    return (
        operations.size === members.length &&
        distinct(members.map((m) => m.source_allocation_line_id)) &&
        distinct(members.map((m) => m.source_operation_id)) &&
        members.every((m) => !operations.has(m.source_operation_id))
    );
}
export function validReturnCurrent(
    row: unknown,
    scope: ReturnScope,
    assets: AssetScales,
): row is ReturnCurrent {
    if (
        !record(row) ||
        !keys(row, [
            "return_id",
            "ledger_id",
            "payment_id",
            "party_id",
            "payment_direction",
            "payment_asset_id",
            "version",
            "status",
            "revision_id",
            "revision_reason",
            "fact",
            "members",
        ]) ||
        !scopeMatches(row, scope) ||
        !uuid(row.party_id) ||
        (row.payment_direction !== "receipt" && row.payment_direction !== "payout") ||
        !version(row.version) ||
        !Array.isArray(row.members) ||
        row.members.length > 200 ||
        (row.version === 1
            ? row.revision_id !== null || row.revision_reason !== null
            : !uuid(row.revision_id) || !reason(row.revision_reason, 1000))
    )
        return false;
    if (row.status === "cancelled")
        return row.version > 1 && row.fact === null && row.members.length === 0;
    if (row.status !== "active" || !businessFact(row.fact) || row.members.length === 0)
        return false;
    const groupVersion = row.version;
    try {
        return (
            row.members.every(
                (m, i) =>
                    record(m) &&
                    keys(m, [
                        ...common,
                        "project_id",
                        "line_no",
                        "journal_id",
                        "payment_amount",
                        "document_amount",
                    ]) &&
                    memberIdentity(m, groupVersion) &&
                    m.line_no === i + 1 &&
                    uuid(m.journal_id) &&
                    (m.project_id === null || uuid(m.project_id)) &&
                    pair(m, scope.payment_asset_id, m.document_asset_id as string, assets),
            ) &&
            identities(row.members) &&
            distinct(row.members.map((m) => m.journal_id))
        );
    } catch {
        return false;
    }
}

type Posting = NonNullable<ReturnRevisionReceipt["members"][number]["replacement"]>;
function posting(
    value: unknown,
    v: number,
    payment: string,
    document: string,
    assets: AssetScales,
): value is Posting {
    return (
        record(value) &&
        keys(value, [
            "line_no",
            "operation_version",
            "journal_id",
            "payment_amount",
            "document_amount",
        ]) &&
        position(value.line_no) &&
        value.operation_version === v &&
        uuid(value.journal_id) &&
        pair(value, payment, document, assets)
    );
}

export function validReturnRevision(
    row: unknown,
    scope: ReturnScope,
    entry: Pick<ReturnHistoryEntry, "version" | "revision_id" | "action" | "reason">,
    assets: AssetScales,
): row is ReturnRevisionReceipt {
    if (
        !record(row) ||
        !keys(row, [
            "kind",
            "return_id",
            "revision_id",
            "ledger_id",
            "payment_id",
            "payment_asset_id",
            "previous_version",
            "version",
            "action",
            "reason",
            "previous",
            "replacement",
            "members",
        ]) ||
        row.kind !== "business_allocation_return_revision" ||
        !scopeMatches(row, scope) ||
        !uuid(row.revision_id) ||
        row.revision_id !== entry.revision_id ||
        !version(row.version) ||
        row.version < 2 ||
        row.version !== entry.version ||
        row.previous_version !== row.version - 1 ||
        row.action !== entry.action ||
        row.reason !== entry.reason ||
        !reason(row.reason, 1000) ||
        !businessFact(row.previous) ||
        (row.action === "cancel"
            ? row.replacement !== null
            : row.action !== "correct" || !businessFact(row.replacement)) ||
        !Array.isArray(row.members) ||
        row.members.length < 1 ||
        row.members.length > 400
    )
        return false;
    const active: number[] = [],
        old: number[] = [],
        removed: number[] = [],
        journals: string[] = [];
    try {
        for (const m of row.members) {
            if (
                !record(m) ||
                !keys(m, [...common, "action", "previous", "reversal_journal_id", "replacement"]) ||
                !memberIdentity(m, row.version)
            )
                return false;
            const v = m.operation_version as number,
                doc = m.document_asset_id as string;
            if (m.action === "create") {
                if (m.previous !== null || m.reversal_journal_id !== null || v !== 1) return false;
            } else {
                if (
                    (m.action !== "correct" && m.action !== "cancel") ||
                    v < 2 ||
                    !posting(m.previous, v - 1, scope.payment_asset_id, doc, assets) ||
                    !uuid(m.reversal_journal_id)
                )
                    return false;
                old.push(m.previous.line_no);
                journals.push(m.previous.journal_id, m.reversal_journal_id);
            }
            if (m.action === "cancel") {
                if (m.replacement !== null || !record(m.previous) || !position(m.previous.line_no))
                    return false;
                removed.push(m.previous.line_no);
            } else {
                if (
                    removed.length ||
                    !posting(m.replacement, v, scope.payment_asset_id, doc, assets)
                )
                    return false;
                active.push(m.replacement.line_no);
                journals.push(m.replacement.journal_id);
            }
        }
        return (
            identities(row.members) &&
            distinct(journals) &&
            old.length > 0 &&
            old.length <= 200 &&
            consecutive(old.sort((a, b) => a - b)) &&
            active.length <= 200 &&
            consecutive(active) &&
            removed.every((n, i) => i === 0 || removed[i - 1] < n) &&
            (row.action === "correct" ? active.length > 0 : active.length === 0)
        );
    } catch {
        return false;
    }
}
