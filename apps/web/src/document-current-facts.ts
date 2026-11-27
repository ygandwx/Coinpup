import type { DocumentCurrent } from "./document-revision-api";
import type { Asset } from "./ledger-api";
import { assetIdentity } from "./asset-identity";
import { priceBusinessDocument } from "./business-pricing";
import { date, keys, record, uuid } from "./credit-intent";
import { parseAmount } from "./money";

const version = (value: unknown): value is number =>
    Number.isSafeInteger(value) && (value as number) >= 1 && (value as number) <= 2147483647;
const nullableText = (value: unknown) => value === null || typeof value === "string";
const amounts = ["net_amount", "tax_amount", "total_amount"] as const;

function snapshotFor(
    value: unknown,
    id: string,
    kind: "customer" | "supplier" | "project" | "income" | "expense",
): value is Record<string, unknown> {
    if (!record(value)) return false;
    const identified = Object.hasOwn(value, "id");
    if (identified !== Object.hasOwn(value, "version")) return false;
    if (identified && (value.id !== id || !version(value.version))) return false;
    if (kind === "customer" || kind === "supplier") {
        return (
            !Object.hasOwn(value, "kind") &&
            ((!identified && !Object.hasOwn(value, "role")) ||
                value.role === kind ||
                value.role === "both")
        );
    }
    if (Object.hasOwn(value, "role")) return false;
    if (kind === "project") return !Object.hasOwn(value, "kind");
    // Historical display-only snapshots can carry a name without a master identity.
    return value.kind === kind || (!identified && !Object.hasOwn(value, "kind"));
}

export function validDocumentAsset(asset: Asset | undefined, id: unknown): asset is Asset {
    return (
        !!asset &&
        record(asset) &&
        asset.asset_id === id &&
        typeof id === "string" &&
        !!id &&
        typeof asset.code === "string" &&
        !!asset.code &&
        typeof asset.enabled === "boolean" &&
        Number.isSafeInteger(asset.scale) &&
        asset.scale >= 0 &&
        asset.scale <= 18 &&
        asset.asset_id === assetIdentity(asset)
    );
}

/** Validate a complete read for display or review. It never proves a command was committed. */
export function validDocumentCurrent(
    value: unknown,
    ledgerId: string,
    documentId: string,
    asset?: Asset,
): value is DocumentCurrent {
    try {
        if (
            !record(value) ||
            !uuid(ledgerId) ||
            !uuid(documentId) ||
            !keys(value, [
                "ledger_id",
                "document_id",
                "confirmation_id",
                "document_kind",
                "version",
                "status",
                "revision_id",
                "document",
                "postings",
            ]) ||
            value.ledger_id !== ledgerId ||
            value.document_id !== documentId ||
            !uuid(value.confirmation_id) ||
            !["invoice", "bill"].includes(value.document_kind as string) ||
            !version(value.version) ||
            value.version < 2 ||
            (value.revision_id !== null && (!uuid(value.revision_id) || value.version < 3)) ||
            !Array.isArray(value.postings) ||
            value.postings.length > 200
        )
            return false;
        if (value.status === "cancelled")
            return (
                value.revision_id !== null && value.document === null && value.postings.length === 0
            );
        const d = value.document;
        if (
            value.status !== "active" ||
            !record(d) ||
            d.id !== documentId ||
            d.ledger_id !== ledgerId ||
            d.confirmation_id !== value.confirmation_id ||
            d.document_kind !== value.document_kind ||
            d.version !== value.version ||
            d.state !== "confirmed" ||
            d.archived !== false ||
            !uuid(d.party_id) ||
            !validDocumentAsset(asset, d.asset_id) ||
            !date(d.issue_date) ||
            (d.due_date !== null && !date(d.due_date)) ||
            !nullableText(d.notes) ||
            !record(d.issuer_snapshot) ||
            !snapshotFor(
                d.party_snapshot,
                d.party_id,
                d.document_kind === "invoice" ? "customer" : "supplier",
            ) ||
            ![d.created_at, d.updated_at, d.confirmed_at].every(
                (v) => typeof v === "string" && !!v,
            ) ||
            !Array.isArray(d.lines) ||
            d.lines.length < 1 ||
            d.lines.length > 200 ||
            d.line_count !== d.lines.length
        )
            return false;
        const categoryKind = d.document_kind === "invoice" ? "income" : "expense";
        const ids = new Set<string>();
        let position = 0;
        for (const row of d.lines) {
            if (
                !record(row) ||
                !uuid(row.id) ||
                ids.has(row.id) ||
                row.document_id !== documentId ||
                row.ledger_id !== ledgerId ||
                row.asset_id !== asset.asset_id ||
                row.archived !== false ||
                !version(row.version) ||
                !version(row.line_no) ||
                row.line_no <= position ||
                !uuid(row.category_id) ||
                row.category_kind !== categoryKind ||
                !snapshotFor(row.category_snapshot, row.category_id, categoryKind) ||
                !date(row.recognition_date) ||
                (row.project_id === null
                    ? row.project_snapshot !== null
                    : !uuid(row.project_id) ||
                      !snapshotFor(row.project_snapshot, row.project_id, "project")) ||
                ![
                    "description",
                    "quantity",
                    "unit_price",
                    "discount_amount",
                    "tax_rate_percent",
                    "created_at",
                    "updated_at",
                ].every((name) => typeof row[name] === "string" && !!row[name])
            )
                return false;
            ids.add(row.id);
            position = row.line_no;
        }
        const prices = priceBusinessDocument(d.lines, asset.scale);
        const positive: string[] = [];
        for (const [i, row] of d.lines.entries()) {
            if (
                !amounts.every(
                    (key) =>
                        parseAmount(row[key], asset.scale) ===
                        parseAmount(prices.lines[i][key], asset.scale),
                )
            )
                return false;
            if (parseAmount(row.total_amount, asset.scale) > 0n) positive.push(row.id);
        }
        if (
            !amounts.every(
                (key) =>
                    parseAmount(d[key] as string, asset.scale) ===
                    parseAmount(prices[key], asset.scale),
            ) ||
            parseAmount(prices.total_amount, asset.scale) <= 0n ||
            positive.length !== value.postings.length
        )
            return false;
        const currentVersion = value.version;
        const operations = new Set<string>(),
            journals = new Set<string>();
        return value.postings.every((p, i) => {
            if (
                !record(p) ||
                !keys(p, ["source_line_id", "operation_id", "operation_version", "journal_id"]) ||
                p.source_line_id !== positive[i] ||
                !uuid(p.operation_id) ||
                !uuid(p.journal_id) ||
                operations.has(p.operation_id) ||
                journals.has(p.journal_id) ||
                !version(p.operation_version) ||
                p.operation_version >= currentVersion ||
                (value.revision_id === null && p.operation_version !== 1)
            )
                return false;
            operations.add(p.operation_id);
            journals.add(p.journal_id);
            return true;
        });
    } catch {
        return false;
    }
}
