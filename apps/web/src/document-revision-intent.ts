import type {
    DocumentCancellation,
    DocumentCorrection,
    DocumentCurrent,
    DocumentReplacement,
} from "./document-revision-api";
import type { Party, Project } from "./business-api";
import type { Asset, Category } from "./ledger-api";
import { validDocumentAsset, validDocumentCurrent } from "./document-current-facts";
import { priceBusinessDocument } from "./business-pricing";
import { date, keys, record, uuid } from "./credit-intent";
import { stripCommandText } from "./command-text";
import { parseAmount } from "./money";

export type DocumentRevisionInput =
    | { action: "correct"; body: DocumentCorrection }
    | { action: "cancel"; body: DocumentCancellation };
export type DocumentReference =
    | { kind: "party"; record: Party }
    | { kind: "category"; record: Category }
    | { kind: "project"; record: Project };
export type DocumentRevisionReview = {
    current: DocumentCurrent;
    oldAsset: Asset;
    newAsset?: Asset;
    references: DocumentReference[];
};
export type FrozenDocumentRevision = Readonly<{
    ownerId: string;
    ledgerId: string;
    documentId: string;
    action: DocumentRevisionInput["action"];
    key: string;
    bodyJson: string;
    reviewedJson: string;
}>;
export function isDocumentRevisionPlan(value: unknown): value is FrozenDocumentRevision {
    return record(value) && (value.action === "correct" || value.action === "cancel");
}
const version = (v: unknown): v is number =>
    Number.isSafeInteger(v) && (v as number) >= 1 && (v as number) <= 2147483647;
const text = (v: unknown, limit: number, nonempty = false): v is string =>
    typeof v === "string" &&
    !v.includes("\0") &&
    [...v].length <= limit &&
    (!nonempty || !!stripCommandText(v));

export function validDocumentRevisionInput(value: unknown): value is DocumentRevisionInput {
    if (!record(value) || !keys(value, ["action", "body"]) || !record(value.body)) return false;
    const b = value.body;
    if (
        !version(b.expected_version) ||
        b.expected_version < 2 ||
        b.expected_version >= 2147483647 ||
        typeof b.reason !== "string" ||
        !text(stripCommandText(b.reason), 1000, true)
    )
        return false;
    if (value.action === "cancel") return keys(b, ["expected_version", "reason"]);
    const r = b.replacement;
    if (
        value.action !== "correct" ||
        !keys(b, ["expected_version", "reason", "replacement"]) ||
        !record(r) ||
        !keys(r, [
            "document_kind",
            "party_id",
            "asset_id",
            "issue_date",
            "due_date",
            "notes",
            "lines",
            "reviewed_references",
        ]) ||
        !["invoice", "bill"].includes(r.document_kind as string) ||
        !uuid(r.party_id) ||
        !text(r.asset_id, 200, true) ||
        !date(r.issue_date) ||
        (r.due_date !== undefined && r.due_date !== null && !date(r.due_date)) ||
        (r.notes !== undefined && r.notes !== null && !text(r.notes, 2000)) ||
        !Array.isArray(r.lines) ||
        r.lines.length < 1 ||
        r.lines.length > 200
    )
        return false;
    const ids = new Set<string>();
    const allowed = new Set([`party:${r.party_id}`]);
    for (const line of r.lines) {
        if (
            !record(line) ||
            !keys(line, [
                "id",
                "description",
                "quantity",
                "unit_price",
                "discount_amount",
                "tax_rate_percent",
                "category_id",
                "project_id",
                "recognition_date",
            ]) ||
            !uuid(line.id) ||
            ids.has(line.id) ||
            !text(line.description, 2000, true) ||
            !uuid(line.category_id) ||
            (line.project_id !== undefined && line.project_id !== null && !uuid(line.project_id)) ||
            !date(line.recognition_date)
        )
            return false;
        ids.add(line.id);
        allowed.add(`category:${line.category_id}`);
        if (line.project_id) allowed.add(`project:${line.project_id}`);
    }
    const refs = r.reviewed_references ?? [];
    if (!Array.isArray(refs) || refs.length > 401) return false;
    const seen = new Set<string>();
    for (const ref of refs) {
        if (
            !record(ref) ||
            !keys(ref, ["kind", "id", "version"]) ||
            !uuid(ref.id) ||
            !version(ref.version)
        )
            return false;
        const key = `${ref.kind}:${ref.id}`;
        if (!allowed.has(key) || seen.has(key)) return false;
        seen.add(key);
    }
    // Input-only check preserves exact text; the reviewed asset supplies final precision.
    try {
        return (
            parseAmount(
                priceBusinessDocument(r.lines as DocumentReplacement["lines"], 18).total_amount,
                18,
            ) > 0n
        );
    } catch {
        return false;
    }
}

function validReferences(
    ledgerId: string,
    replacement: DocumentReplacement,
    review: DocumentRevisionReview,
) {
    const pins = replacement.reviewed_references ?? [];
    if (!Array.isArray(review.references) || review.references.length !== pins.length) return false;
    const selected = new Map<string, DocumentReference>();
    const partyRole = replacement.document_kind === "invoice" ? "customer" : "supplier";
    for (const item of review.references) {
        if (!record(item) || !record(item.record)) return false;
        const row = item.record,
            key = `${item.kind}:${row.id}`;
        const pin = pins.find((p) => p.kind === item.kind && p.id === row.id);
        if (
            selected.has(key) ||
            !pin ||
            row.version !== pin.version ||
            row.ledger_id !== ledgerId ||
            row.archived !== false ||
            !text(row.name, 200, true) ||
            (item.kind === "party" &&
                item.record.role !== partyRole &&
                item.record.role !== "both") ||
            (item.kind === "category" &&
                item.record.kind !==
                    (replacement.document_kind === "invoice" ? "income" : "expense"))
        )
            return false;
        selected.set(key, item);
    }
    const prior = review.current.document!;
    if (prior.party_id !== replacement.party_id && !selected.has(`party:${replacement.party_id}`))
        return false;
    return replacement.lines.every((row) => {
        const old = prior.lines.find((p) => p.id === row.id);
        return (
            (old?.category_id === row.category_id || selected.has(`category:${row.category_id}`)) &&
            (row.project_id == null ||
                old?.project_id === row.project_id ||
                selected.has(`project:${row.project_id}`))
        );
    });
}

/** A review freezes visible facts; only the write transaction decides live dependencies. */
export function validDocumentRevisionReview(
    ledgerId: string,
    documentId: string,
    input: unknown,
    value: unknown,
): value is DocumentRevisionReview {
    try {
        if (!validDocumentRevisionInput(input) || !record(value)) return false;
        const review = value as unknown as DocumentRevisionReview;
        if (
            !validDocumentCurrent(review.current, ledgerId, documentId, review.oldAsset) ||
            review.current.status !== "active" ||
            review.current.version !== input.body.expected_version
        )
            return false;
        // Pure cancellation must still work with disabled historical references.
        if (input.action === "cancel") return true;
        const r = input.body.replacement;
        return (
            r.document_kind === review.current.document_kind &&
            validDocumentAsset(review.newAsset, r.asset_id) &&
            review.newAsset.enabled &&
            validReferences(ledgerId, r, review) &&
            parseAmount(
                priceBusinessDocument(r.lines, review.newAsset.scale).total_amount,
                review.newAsset.scale,
            ) > 0n
        );
    } catch {
        return false;
    }
}
