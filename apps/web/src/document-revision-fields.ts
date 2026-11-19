import type { DocumentCurrent } from "./document-revision-api";
import type {
    DocumentReference,
    DocumentRevisionInput,
    DocumentRevisionReview,
} from "./document-revision-intent";
import { validDocumentRevisionReview } from "./document-revision-intent";
import { draftFields } from "./draft-fields";
import type { DraftFields, DraftReferences } from "./draft-fields";

export type DocumentRevisionFields = {
    action: "correct" | "cancel";
    reason: string;
    replacement: DraftFields | null;
    refreshReferences: boolean;
};
export type ReviewedDocumentRevision = {
    input: DocumentRevisionInput;
    review: DocumentRevisionReview;
};

export function documentRevisionFields(
    current: DocumentCurrent,
    action: "correct" | "cancel",
): DocumentRevisionFields {
    if (current.status !== "active" || !current.document) throw new Error("document_not_active");
    return {
        action,
        reason: "",
        replacement: action === "correct" ? draftFields(current.document) : null,
        refreshReferences: false,
    };
}

/** Preserve each unchanged historical reference; only new/changed or explicitly refreshed references are pinned. */
export function reviewDocumentRevision(
    current: DocumentCurrent,
    fields: DocumentRevisionFields,
    refs: DraftReferences,
): ReviewedDocumentRevision | null {
    try {
        const old = current.document;
        if (current.status !== "active" || !old || typeof fields.refreshReferences !== "boolean")
            return null;
        const asset = (identity: string) => {
            const found = refs.assets.filter((row) => row.asset_id === identity);
            if (found.length !== 1) throw new Error("ambiguous_asset");
            return found[0];
        };
        const review: DocumentRevisionReview = {
            current,
            oldAsset: asset(old.asset_id),
            references: [],
        };
        let input: DocumentRevisionInput;
        if (fields.action === "cancel") {
            if (fields.replacement !== null || fields.refreshReferences) return null;
            input = {
                action: "cancel",
                body: { expected_version: current.version, reason: fields.reason },
            };
        } else {
            const replacement = fields.replacement;
            if (
                fields.action !== "correct" ||
                !replacement ||
                replacement.document_kind !== current.document_kind
            )
                return null;
            review.newAsset = asset(replacement.asset_id);
            const catalog: DocumentReference[] = [
                ...refs.parties.map((record) => ({ kind: "party" as const, record })),
                ...refs.categories.map((record) => ({ kind: "category" as const, record })),
                ...refs.projects.map((record) => ({ kind: "project" as const, record })),
            ];
            const pins = new Map<string, DocumentReference>();
            const indexed = new Map<string, DocumentReference[]>();
            for (const entry of catalog) {
                const key = `${entry.kind}:${entry.record.id}`;
                indexed.set(key, [...(indexed.get(key) ?? []), entry]);
            }
            const reference = (
                kind: DocumentReference["kind"],
                identity: string,
                refreshed: boolean,
            ) => {
                const rows = indexed.get(`${kind}:${identity}`) ?? [];
                if (rows.length !== 1) throw new Error("ambiguous_reference");
                const entry = rows[0],
                    row = entry.record;
                if (
                    row.ledger_id !== current.ledger_id ||
                    row.archived !== false ||
                    typeof row.name !== "string" ||
                    !row.name ||
                    !Number.isSafeInteger(row.version) ||
                    row.version < 1 ||
                    row.version > 2147483647
                )
                    throw new Error("unavailable_reference");
                if (
                    entry.kind === "party" &&
                    ![
                        replacement.document_kind === "invoice" ? "customer" : "supplier",
                        "both",
                    ].includes(entry.record.role ?? "")
                )
                    throw new Error("party_role");
                if (
                    entry.kind === "category" &&
                    entry.record.kind !==
                        (replacement.document_kind === "invoice" ? "income" : "expense")
                )
                    throw new Error("category_kind");
                if (refreshed) pins.set(`${kind}:${identity}`, entry);
            };
            reference(
                "party",
                replacement.party_id,
                fields.refreshReferences || old.party_id !== replacement.party_id,
            );
            for (const row of replacement.lines) {
                const previous = old.lines.find((line) => line.id === row.id);
                reference(
                    "category",
                    row.category_id,
                    fields.refreshReferences || previous?.category_id !== row.category_id,
                );
                if (row.project_id)
                    reference(
                        "project",
                        row.project_id,
                        fields.refreshReferences || previous?.project_id !== row.project_id,
                    );
            }
            review.references = [...pins.values()];
            input = {
                action: "correct",
                body: {
                    expected_version: current.version,
                    reason: fields.reason,
                    replacement: {
                        ...replacement,
                        reviewed_references: review.references.map(({ kind, record }) => ({
                            kind,
                            id: record.id,
                            version: record.version,
                        })),
                    },
                },
            };
        }
        if (!validDocumentRevisionReview(current.ledger_id, current.document_id, input, review))
            return null;
        // The preview must not drift when a parent refreshes a catalog or editor object.
        return JSON.parse(JSON.stringify({ input, review })) as ReviewedDocumentRevision;
    } catch {
        return null;
    }
}
