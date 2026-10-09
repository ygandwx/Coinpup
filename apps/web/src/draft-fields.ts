import type { BusinessDraft, BusinessDraftCreate } from "./business-draft-api";
import type { Asset, Category } from "./ledger-api";
import type { Party, Project } from "./business-api";
import { priceBusinessDocument } from "./business-pricing";
import { masterName } from "./pending-business";

export type DraftLine = NonNullable<BusinessDraftCreate["lines"]>[number];
export type DraftFields = Omit<BusinessDraftCreate, "id" | "lines"> & {
    lines: DraftLine[];
};
export type DraftReferences = {
    assets: Asset[];
    categories: Category[];
    parties: Party[];
    projects: Project[];
};
export function draftFields(value: BusinessDraftCreate | BusinessDraft): DraftFields {
    return {
        document_kind: value.document_kind,
        party_id: value.party_id,
        asset_id: value.asset_id,
        issue_date: value.issue_date,
        due_date: value.due_date ?? null,
        notes: value.notes ?? null,
        lines: (value.lines ?? []).map((line) => ({
            id: line.id,
            description: line.description,
            quantity: line.quantity,
            unit_price: line.unit_price,
            discount_amount: line.discount_amount ?? "0",
            tax_rate_percent: line.tax_rate_percent ?? "0",
            category_id: line.category_id,
            project_id: line.project_id ?? null,
            recognition_date: line.recognition_date,
        })),
    };
}
export function newDraft(date: string): DraftFields {
    return {
        document_kind: "invoice",
        party_id: "",
        asset_id: "",
        issue_date: date,
        due_date: null,
        notes: null,
        lines: [],
    };
}
export function newDraftLine(id: string, date: string): DraftLine {
    return {
        id,
        description: "",
        quantity: "1",
        unit_price: "0",
        discount_amount: "0",
        tax_rate_percent: "0",
        category_id: "",
        project_id: null,
        recognition_date: date,
    };
}
export function localDraftDate(): string {
    const date = new Date();
    return `${date.getFullYear().toString().padStart(4, "0")}-${(date.getMonth() + 1).toString().padStart(2, "0")}-${date.getDate().toString().padStart(2, "0")}`;
}
export function validDraftDate(value: string): boolean {
    if (!/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/u.test(value)) return false;
    const [year, month, day] = value.split("-").map(Number);
    const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    return (
        year >= 1 &&
        year <= 9999 &&
        month >= 1 &&
        month <= 12 &&
        day >= 1 &&
        day <= [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    );
}
export function validDraftFields(value: DraftFields, refs: DraftReferences): boolean {
    const party = refs.parties.find((row) => row.id === value.party_id);
    const asset = refs.assets.find((row) => row.asset_id === value.asset_id);
    const kind = value.document_kind === "invoice" ? "income" : "expense";
    if (
        !["invoice", "bill"].includes(value.document_kind) ||
        !asset?.enabled ||
        !party ||
        party.archived ||
        !party.name ||
        ![value.document_kind === "invoice" ? "customer" : "supplier", "both"].includes(
            party.role ?? "",
        ) ||
        !validDraftDate(value.issue_date) ||
        (value.due_date != null && !validDraftDate(value.due_date)) ||
        (value.notes != null && (value.notes.length > 2000 || value.notes.includes("\0")))
    )
        return false;
    if (
        new Set(value.lines.map((line) => line.id)).size !== value.lines.length ||
        value.lines.some((line) => {
            const category = refs.categories.find((row) => row.id === line.category_id);
            const project = line.project_id
                ? refs.projects.find((row) => row.id === line.project_id)
                : null;
            return (
                !line.id ||
                !masterName(line.description) ||
                line.description.length > 2000 ||
                line.description.includes("\0") ||
                !validDraftDate(line.recognition_date) ||
                !category ||
                category.archived ||
                category.kind !== kind ||
                (!!line.project_id && (!project || project.archived))
            );
        })
    )
        return false;
    try {
        priceBusinessDocument(value.lines, asset.scale);
        return true;
    } catch {
        return false;
    }
}
