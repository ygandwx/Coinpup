import type {
    DocumentCurrent,
    DocumentReplacement,
    DocumentRevisionReceipt,
} from "./document-revision-api";
import type {
    DocumentRevisionInput,
    DocumentRevisionReview,
    FrozenDocumentRevision,
} from "./document-revision-intent";
import { validDocumentRevisionReview } from "./document-revision-intent";
import { priceBusinessDocument } from "./business-pricing";
import { keys, record, uuid } from "./credit-intent";
import { stripCommandText } from "./command-text";
import { parseAmount } from "./money";

type Group = DocumentRevisionReceipt["previous"];
type Source = DocumentReplacement | NonNullable<DocumentCurrent["document"]>;
const headers = [
    "document_kind",
    "party_id",
    "asset_id",
    "issue_date",
    "due_date",
    "notes",
] as const;
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
] as const;
const amounts = ["net_amount", "tax_amount", "total_amount"] as const;
const postingKeys = ["operation_id", "operation_version", "journal_id"];
function sameAmounts(a: Record<string, unknown>, b: Record<string, unknown>): boolean {
    return amounts.every(
        (key) =>
            typeof a[key] === "string" &&
            !a[key].startsWith("-") &&
            parseAmount(a[key], 18) === parseAmount(b[key] as string, 18),
    );
}
function completeGroup(
    value: unknown,
    source: Source,
    scale: number,
    previous: boolean,
): value is Group {
    if (
        !record(value) ||
        !keys(value, [...headers, ...amounts, "lines"]) ||
        !Array.isArray(value.lines) ||
        value.lines.length !== source.lines.length
    )
        return false;
    const expected = { due_date: null, notes: null, ...source };
    if (!headers.every((key) => value[key] === expected[key])) return false;
    const prices = priceBusinessDocument(source.lines, scale);
    if (!sameAmounts(value, prices)) return false;
    return value.lines.every((row, i) => {
        if (!record(row) || !keys(row, [...fields, ...amounts, "line_no", "posting"])) return false;
        const src = source.lines[i];
        const normalized = {
            discount_amount: "0",
            tax_rate_percent: "0",
            project_id: null,
            ...src,
            description: stripCommandText(src.description),
        };
        return (
            fields.every((key) => row[key] === normalized[key]) &&
            row.line_no ===
                (previous
                    ? (src as NonNullable<DocumentCurrent["document"]>["lines"][number]).line_no
                    : i + 1) &&
            sameAmounts(row, prices.lines[i]) &&
            (parseAmount(prices.lines[i].total_amount, scale) === 0n
                ? row.posting === null
                : record(row.posting) && keys(row.posting, postingKeys))
        );
    });
}

/** Validate only the complete response to this frozen command, never a later current read. */
export function validDocumentRevisionReceipt(
    value: unknown,
    plan: FrozenDocumentRevision,
): value is DocumentRevisionReceipt {
    try {
        const input = {
            action: plan.action,
            body: JSON.parse(plan.bodyJson),
        } as DocumentRevisionInput;
        const review = JSON.parse(plan.reviewedJson) as DocumentRevisionReview;
        if (
            !validDocumentRevisionReview(plan.ledgerId, plan.documentId, input, review) ||
            !record(value)
        )
            return false;
        const c = review.current;
        if (
            !keys(value, [
                "kind",
                "ledger_id",
                "confirmation_id",
                "document_id",
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
            value.ledger_id !== plan.ledgerId ||
            value.document_id !== plan.documentId ||
            value.confirmation_id !== c.confirmation_id ||
            !uuid(value.revision_id) ||
            value.revision_id === c.revision_id ||
            value.previous_version !== c.version ||
            value.version !== c.version + 1 ||
            value.action !== input.action ||
            value.reason !== stripCommandText(input.body.reason) ||
            !completeGroup(value.previous, c.document!, review.oldAsset.scale, true) ||
            !Array.isArray(value.reversals) ||
            value.reversals.length !== c.postings.length
        )
            return false;
        const prior = new Map(c.postings.map((p) => [p.source_line_id, p]));
        const priorOperations = new Set(c.postings.map((p) => p.operation_id));
        const journals = new Set<string>();
        const newJournal = (id: unknown): id is string => {
            if (!uuid(id) || journals.has(id)) return false;
            journals.add(id);
            return true;
        };
        for (const row of value.previous.lines) {
            const p = prior.get(row.id);
            if (!p) {
                if (row.posting !== null) return false;
                continue;
            }
            if (
                !row.posting ||
                row.posting.operation_id !== p.operation_id ||
                row.posting.operation_version !== p.operation_version ||
                row.posting.journal_id !== p.journal_id ||
                !newJournal(row.posting.journal_id)
            )
                return false;
        }
        for (const [i, r] of value.reversals.entries()) {
            if (
                !record(r) ||
                !keys(r, ["source_line_id", "journal_id"]) ||
                r.source_line_id !== c.postings[i].source_line_id ||
                !newJournal(r.journal_id)
            )
                return false;
        }
        if (input.action === "cancel") return value.replacement === null;
        if (
            !completeGroup(value.replacement, input.body.replacement, review.newAsset!.scale, false)
        )
            return false;
        const operations = new Set<string>();
        return value.replacement.lines.every((row) => {
            if (row.posting === null) return true;
            const p = row.posting,
                old = prior.get(row.id);
            if (
                !uuid(p.operation_id) ||
                operations.has(p.operation_id) ||
                !newJournal(p.journal_id) ||
                (old
                    ? p.operation_id !== old.operation_id ||
                      p.operation_version !== old.operation_version + 1
                    : priorOperations.has(p.operation_id) || p.operation_version !== 1)
            )
                return false;
            operations.add(p.operation_id);
            return true;
        });
    } catch {
        return false;
    }
}
