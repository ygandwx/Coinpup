import type { DocumentRevisionReceipt } from "./document-revision-api";
import type {
    FrozenDocumentRevision,
    DocumentRevisionInput,
    DocumentRevisionReview,
} from "./document-revision-intent";
import { validDocumentRevisionReview } from "./document-revision-intent";
import { validDocumentRevisionReceipt } from "./document-revision-receipt";
import { priceBusinessDocument } from "./business-pricing";
import type { PendingStatus } from "./pending-command";
import type { DeepReadonly } from "./settlement-intent";

type Group = DocumentRevisionReceipt["previous"];
export type DocumentDisplayGroup = Omit<Group, "lines"> & {
    lines: (Omit<Group["lines"][number], "posting"> & {
        posting?: Group["lines"][number]["posting"];
    })[];
};
export function documentRevisionDisplay(
    plan: FrozenDocumentRevision,
    status: PendingStatus,
    receipt: DeepReadonly<DocumentRevisionReceipt> | null,
) {
    try {
        const input = {
            action: plan.action,
            body: JSON.parse(plan.bodyJson),
        } as DocumentRevisionInput;
        const review = JSON.parse(plan.reviewedJson) as DocumentRevisionReview;
        if (!validDocumentRevisionReview(plan.ledgerId, plan.documentId, input, review))
            return null;
        if (status === "confirmed") {
            if (!validDocumentRevisionReceipt(receipt, plan)) return null;
            const saved = receipt as DocumentRevisionReceipt;
            return {
                input,
                review,
                saved,
                replacement: saved.replacement as DocumentDisplayGroup | null,
            };
        }
        let replacement: DocumentDisplayGroup | null = null;
        if (input.action === "correct") {
            const raw = input.body.replacement,
                price = priceBusinessDocument(raw.lines, review.newAsset!.scale);
            replacement = {
                ...raw,
                due_date: raw.due_date ?? null,
                notes: raw.notes ?? null,
                ...price,
                lines: raw.lines.map((row, i) => ({ ...row, line_no: i + 1, ...price.lines[i] })),
            };
        }
        return { input, review, saved: null, replacement };
    } catch {
        return null;
    }
}
