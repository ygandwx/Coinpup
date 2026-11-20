import type {
    DocumentCurrent,
    DocumentHistoryEntry,
    DocumentRevisionReceipt,
} from "./document-revision-api";
import type { Asset } from "./ledger-api";
import { validDocumentAsset, validDocumentCurrent } from "./document-current-facts";
import { validDocumentRevisionInput } from "./document-revision-intent";
import { priceBusinessDocument } from "./business-pricing";
import { keys, record, uuid } from "./credit-intent";
import { stripCommandText } from "./command-text";
import { parseAmount } from "./money";
export type DocumentHistoryScope = {
    ledgerId: string;
    documentId: string;
    confirmationId: string;
    originalVersion: number;
};
const version = (v: unknown): v is number =>
    Number.isSafeInteger(v) && (v as number) >= 2 && (v as number) <= 2147483647;
const scopeValid = (s: DocumentHistoryScope) =>
    uuid(s.ledgerId) && uuid(s.documentId) && uuid(s.confirmationId) && version(s.originalVersion);
/** Reconstruct original v1 accrual facts only from the permanent confirmation and its sealed document. */
export function originalDocumentFacts(
    value: unknown,
    ledgerId: string,
    documentId: string,
    assets: Asset[],
): DocumentCurrent | null {
    try {
        if (
            !record(value) ||
            !keys(value, ["document", "confirmation"]) ||
            !record(value.document) ||
            !record(value.confirmation)
        )
            return null;
        const d = value.document,
            c = value.confirmation;
        if (
            c.kind !== "business_confirmation" ||
            c.ledger_id !== ledgerId ||
            c.document_id !== documentId ||
            !uuid(c.id) ||
            d.confirmation_id !== c.id ||
            !version(c.document_version) ||
            c.source_version !== c.document_version - 1 ||
            d.version !== c.document_version ||
            !Array.isArray(c.postings) ||
            !Array.isArray(d.lines)
        )
            return null;
        const lines = d.lines;
        const current = {
            ledger_id: ledgerId,
            document_id: documentId,
            confirmation_id: c.id,
            document_kind: d.document_kind,
            version: c.document_version,
            status: "active",
            revision_id: null,
            document: d,
            postings: c.postings.map((p) => {
                if (
                    !record(p) ||
                    !keys(p, ["line_id", "line_no", "operation_id", "journal_id"]) ||
                    !lines.some(
                        (r: unknown) => record(r) && r.id === p.line_id && r.line_no === p.line_no,
                    )
                )
                    throw Error("invalid original mapping");
                return {
                    source_line_id: p.line_id,
                    operation_id: p.operation_id,
                    operation_version: 1,
                    journal_id: p.journal_id,
                };
            }),
        };
        const matches = assets.filter((a) => a.asset_id === d.asset_id);
        return matches.length === 1 &&
            validDocumentCurrent(current, ledgerId, documentId, matches[0])
            ? current
            : null;
    } catch {
        return null;
    }
}
export function validDocumentHistoryEntry(
    value: unknown,
    scope: DocumentHistoryScope,
): value is DocumentHistoryEntry {
    if (
        !scopeValid(scope) ||
        !record(value) ||
        !keys(value, [
            "version",
            "revision_id",
            "action",
            "reason",
            "created_at",
            "previous_line_count",
            "replacement_line_count",
        ]) ||
        !version(value.version) ||
        value.version < scope.originalVersion ||
        typeof value.created_at !== "string" ||
        !Number.isFinite(Date.parse(value.created_at))
    )
        return false;
    return value.version === scope.originalVersion
        ? value.action === "confirm" &&
              value.reason === null &&
              value.revision_id === null &&
              value.previous_line_count === null &&
              value.replacement_line_count === null
        : Number.isSafeInteger(value.previous_line_count) &&
              (value.previous_line_count as number) >= 1 &&
              (value.previous_line_count as number) <= 200 &&
              Number.isSafeInteger(value.replacement_line_count) &&
              (value.replacement_line_count as number) >= 0 &&
              (value.replacement_line_count as number) <= 200 &&
              (value.replacement_line_count === 0) === (value.action === "cancel") &&
              ["correct", "cancel"].includes(value.action as string) &&
              uuid(value.revision_id) &&
              typeof value.reason === "string" &&
              !!stripCommandText(value.reason) &&
              value.reason === stripCommandText(value.reason) &&
              !value.reason.includes("\0") &&
              [...value.reason].length <= 1000;
}
export function validDocumentHistoryPage(
    value: unknown,
    scope: DocumentHistoryScope,
    offset: number,
): value is DocumentHistoryEntry[] {
    return (
        scopeValid(scope) &&
        Number.isSafeInteger(offset) &&
        offset >= 0 &&
        offset <= 100000 &&
        offset % 25 === 0 &&
        Array.isArray(value) &&
        value.length <= 25 &&
        value.every(
            (r, i) =>
                validDocumentHistoryEntry(r, scope) &&
                r.version === scope.originalVersion + offset + i &&
                (r.action !== "cancel" || i === value.length - 1),
        ) &&
        new Set(value.map((r) => r.revision_id)).size === value.length
    );
}
type Group = DocumentRevisionReceipt["previous"];
const headers = ["document_kind", "party_id", "asset_id", "issue_date", "due_date", "notes"];
const fields = [
    "id",
    "description",
    "quantity",
    "unit_price",
    "discount_amount",
    "tax_rate_percent",
    "category_id",
    "project_id",
    "recognition_date",
];
const amounts = ["net_amount", "tax_amount", "total_amount"] as const;
const pick = (row: Record<string, unknown>, names: string[]) =>
    Object.fromEntries(names.filter((k) => row[k] !== undefined).map((k) => [k, row[k]]));
function group(
    value: unknown,
    assets: Asset[],
    prior: boolean,
    maximumOperationVersion: number,
    sparse: boolean,
): value is Group {
    if (
        !record(value) ||
        !keys(value, [...headers, ...amounts, "lines"]) ||
        !Array.isArray(value.lines)
    )
        return false;
    const found = assets.filter((a) => a.asset_id === value.asset_id);
    if (found.length !== 1 || !validDocumentAsset(found[0], value.asset_id)) return false;
    const asset = found[0],
        replacement = {
            ...pick(value, headers),
            reviewed_references: [],
            lines: value.lines.map((r) => (record(r) ? pick(r, fields) : r)),
        };
    // This checks raw input shape, not a command outcome. No owner, key or pending intent is constructed.
    if (
        !validDocumentRevisionInput({
            action: "correct",
            body: { expected_version: 2, reason: "Historical group validation", replacement },
        })
    )
        return false;
    const price = priceBusinessDocument(value.lines, asset.scale);
    const same = (row: Record<string, unknown>, expected: Record<string, unknown>) =>
        amounts.every(
            (k) =>
                typeof row[k] === "string" &&
                !row[k].startsWith("-") &&
                parseAmount(row[k], asset.scale) ===
                    parseAmount(expected[k] as string, asset.scale),
        );
    if (!same(value, price) || parseAmount(price.total_amount, asset.scale) <= 0n) return false;
    const operations = new Set(),
        journals = new Set();
    let last = 0;
    return value.lines.every((r, i) => {
        if (
            !record(r) ||
            !keys(r, [...fields, ...amounts, "line_no", "posting"]) ||
            !Number.isSafeInteger(r.line_no) ||
            (r.line_no as number) <= last ||
            (r.line_no as number) > 2147483647 ||
            (!sparse && r.line_no !== i + 1) ||
            r.description !== stripCommandText(r.description as string) ||
            !same(r, price.lines[i])
        )
            return false;
        last = r.line_no as number;
        if (parseAmount(price.lines[i].total_amount, asset.scale) === 0n) return r.posting === null;
        const p = r.posting;
        if (
            !record(p) ||
            !keys(p, ["operation_id", "operation_version", "journal_id"]) ||
            !uuid(p.operation_id) ||
            !uuid(p.journal_id) ||
            operations.has(p.operation_id) ||
            journals.has(p.journal_id) ||
            !Number.isSafeInteger(p.operation_version) ||
            (p.operation_version as number) < 1 ||
            (p.operation_version as number) > maximumOperationVersion ||
            (!prior && (p.operation_version as number) > 2147483647)
        )
            return false;
        operations.add(p.operation_id);
        journals.add(p.journal_id);
        return true;
    });
}
/** Fixed-history display only: it cannot resolve a pending write or verify its original request body. */
export function validDocumentHistoryReceipt(
    value: unknown,
    scope: DocumentHistoryScope,
    selected: DocumentHistoryEntry,
    assets: Asset[],
): value is DocumentRevisionReceipt {
    try {
        if (
            !validDocumentHistoryEntry(selected, scope) ||
            selected.action === "confirm" ||
            !record(value) ||
            !keys(value, [
                "kind",
                "ledger_id",
                "document_id",
                "confirmation_id",
                "revision_id",
                "previous_version",
                "version",
                "action",
                "reason",
                "previous",
                "replacement",
                "reversals",
            ]) ||
            value.kind !== "business_document_revision" ||
            value.ledger_id !== scope.ledgerId ||
            value.document_id !== scope.documentId ||
            value.confirmation_id !== scope.confirmationId ||
            value.revision_id !== selected.revision_id ||
            value.version !== selected.version ||
            value.previous_version !== selected.version - 1 ||
            value.action !== selected.action ||
            value.reason !== selected.reason ||
            !group(
                value.previous,
                assets,
                true,
                selected.version - scope.originalVersion,
                value.previous_version === scope.originalVersion,
            ) ||
            !Array.isArray(value.reversals)
        )
            return false;
        if (value.previous.lines.length !== selected.previous_line_count) return false;
        const before = value.previous.lines.filter((r) => r.posting !== null),
            operations = new Map(before.map((r) => [r.id, r.posting!]));
        if (value.reversals.length !== before.length) return false;
        const allJournals = new Set(before.map((r) => r.posting!.journal_id));
        const distinct = (id: unknown) => {
            if (!uuid(id) || allJournals.has(id)) return false;
            allJournals.add(id);
            return true;
        };
        for (const [i, r] of value.reversals.entries())
            if (
                !record(r) ||
                !keys(r, ["source_line_id", "journal_id"]) ||
                r.source_line_id !== before[i].id ||
                !distinct(r.journal_id)
            )
                return false;
        if (value.action === "cancel") return value.replacement === null;
        if (
            !group(
                value.replacement,
                assets,
                false,
                selected.version - scope.originalVersion + 1,
                false,
            ) ||
            value.replacement.document_kind !== value.previous.document_kind ||
            value.replacement.lines.length !== selected.replacement_line_count
        )
            return false;
        const priorOperations = new Set(before.map((r) => r.posting!.operation_id));
        return value.replacement.lines.every((r) => {
            if (!r.posting) return true;
            const old = operations.get(r.id),
                p = r.posting;
            return (
                distinct(p.journal_id) &&
                (old
                    ? p.operation_id === old.operation_id &&
                      p.operation_version === old.operation_version + 1
                    : !priorOperations.has(p.operation_id) && p.operation_version === 1)
            );
        });
    } catch {
        return false;
    }
}
