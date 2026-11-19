import { ApiError } from "./api";
import { getDocumentCurrent } from "./document-revision-api";
import type { DocumentCurrent } from "./document-revision-api";
import { validDocumentCurrent } from "./document-current-facts";
import { listMasterData } from "./business-api";
import type { MasterKind, MasterRecord, Party, Project } from "./business-api";
import type { Asset, Category } from "./ledger-api";
import type { DraftReferences } from "./draft-fields";
import { documentRevisionFields } from "./document-revision-fields";
import type { DocumentRevisionFields } from "./document-revision-fields";
import { record, uuid } from "./credit-intent";

const invalid = () => new ApiError("server", 200, "invalid_response");
const unabort = (signal: AbortSignal) => signal.throwIfAborted();
export function checkedDocumentCurrent(
    value: unknown,
    ledgerId: string,
    documentId: string,
    assets: Asset[],
): DocumentCurrent {
    const id = record(value) && record(value.document) ? value.document.asset_id : undefined;
    const matches = assets.filter((row) => row.asset_id === id);
    if (matches.length > 1 || !validDocumentCurrent(value, ledgerId, documentId, matches[0]))
        throw invalid();
    return value;
}
function validMaster(row: unknown, ledgerId: string): row is MasterRecord {
    return (
        record(row) &&
        uuid(row.id) &&
        row.ledger_id === ledgerId &&
        Number.isSafeInteger(row.version) &&
        (row.version as number) >= 1 &&
        (row.version as number) <= 2147483647 &&
        typeof row.name === "string" &&
        !!row.name &&
        typeof row.archived === "boolean"
    );
}
async function allMasters(ledgerId: string, kind: MasterKind, signal: AbortSignal) {
    const rows: MasterRecord[] = [],
        seen = new Set<string>();
    for (let offset = 0; ; offset += 25) {
        unabort(signal);
        const page = await listMasterData(ledgerId, kind, offset, signal);
        unabort(signal);
        if (!Array.isArray(page) || page.length > 25) throw invalid();
        for (const row of page) {
            if (
                !validMaster(row, ledgerId) ||
                seen.has(row.id) ||
                (kind === "parties" &&
                    !("role" in row && ["customer", "supplier", "both"].includes(row.role ?? "")))
            )
                throw invalid();
            seen.add(row.id);
            rows.push(row);
        }
        if (page.length < 25) return rows;
    }
}
export type DocumentRevisionEditor = {
    current: DocumentCurrent;
    fields: DocumentRevisionFields;
    refs: DraftReferences;
};
/** Read a complete new review. These reads never reconcile a pending write. */
export async function loadDocumentRevisionEditor(
    ledgerId: string,
    documentId: string,
    action: "correct" | "cancel",
    assets: Asset[],
    categories: Category[],
    signal: AbortSignal,
    expectedVersion?: number,
): Promise<DocumentRevisionEditor> {
    unabort(signal);
    const frozen = structuredClone({ assets, categories });
    const current = checkedDocumentCurrent(
        await getDocumentCurrent(ledgerId, documentId, signal),
        ledgerId,
        documentId,
        frozen.assets,
    );
    unabort(signal);
    if (current.status !== "active") throw new ApiError("server", 409, "document_cancelled");
    if (current.version >= 2147483647)
        throw new ApiError("server", 409, "document_revision_version");
    if (expectedVersion !== undefined && expectedVersion !== current.version)
        throw new ApiError("server", 409, "version_conflict");
    const refs: DraftReferences = { ...frozen, parties: [], projects: [] };
    if (action === "correct") {
        const seen = new Set<string>();
        for (const row of refs.categories) {
            if (
                !validMaster(row, ledgerId) ||
                seen.has(row.id) ||
                !["income", "expense"].includes(row.kind)
            )
                throw invalid();
            seen.add(row.id);
        }
        const [parties, projects] = await Promise.all([
            allMasters(ledgerId, "parties", signal),
            allMasters(ledgerId, "projects", signal),
        ]);
        refs.parties = parties as Party[];
        refs.projects = projects as Project[];
    }
    unabort(signal);
    return { current, fields: documentRevisionFields(current, action), refs };
}
