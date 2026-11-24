import { ApiError, writeJson } from "./api";
import { keys, record, uuid } from "./credit-intent";
import { stripCommandText } from "./command-text";
import { validInvoicePolicy } from "./invoice-issuance-receipt";
import { validReviewedInvoiceProfile } from "./invoice-web-intent";
import type { InvoiceProfile } from "./invoice-issuance-receipt";
import type { components } from "./generated/openapi";
import type { StoredFile } from "./files-api";
import type { MasterIdentity } from "./versioned-master-controller";
export type ProfileCreate = components["schemas"]["InvoiceProfileCreate"];
export type ProfileReplace = components["schemas"]["InvoiceProfileReplacement"];
export type ProfileArchive = components["schemas"]["InvoiceProfileArchive"];
export type ProfileAction = "create" | "replace" | "archive";
export type ProfileBody = ProfileCreate | ProfileReplace | ProfileArchive;
export type FrozenProfile = MasterIdentity &
    Readonly<{ action: ProfileAction; previousJson: string | null }>;
const fields = [
    "name",
    "numbering",
    "issuer_extra_text",
    "footer",
    "payment_instructions",
    "logo_file_id",
    "logo_file_version",
];
const version = (v: unknown): v is number =>
    typeof v === "number" && Number.isInteger(v) && v > 0 && v <= 2147483647;
const id = (v: unknown): v is string => uuid(v) && v.length === 36;
export function freezeProfile(
    ownerId: string,
    ledgerId: string,
    identifier: string,
    action: ProfileAction,
    body: unknown,
    previous: InvoiceProfile | null,
    logo: StoredFile | null = null,
): FrozenProfile {
    const invalid = () => new ApiError("server", null, "invalid_invoice_profile_intent");
    if (
        !id(ownerId) ||
        !id(ledgerId) ||
        !id(identifier) ||
        !record(body) ||
        !["create", "replace", "archive"].includes(action)
    )
        throw invalid();
    if (
        action === "create"
            ? previous !== null || body.id !== identifier
            : !validReviewedInvoiceProfile(previous, ledgerId) ||
              previous.id !== identifier ||
              !version(body.expected_version) ||
              body.expected_version >= 2147483647 ||
              previous.version !== body.expected_version
    )
        throw invalid();
    if (action === "archive") {
        if (
            !keys(body, ["expected_version", "archived"]) ||
            typeof body.archived !== "boolean" ||
            body.archived === previous?.archived ||
            logo !== null
        )
            throw invalid();
    } else {
        if (
            !keys(body, [...fields, action === "create" ? "id" : "expected_version"]) ||
            typeof body.name !== "string" ||
            !stripCommandText(body.name) ||
            Array.from(body.name).length > 160 ||
            [...stripCommandText(body.name)].some((c) => c.codePointAt(0)! < 32) ||
            !validInvoicePolicy(body.numbering) ||
            ![body.issuer_extra_text, body.footer, body.payment_instructions].every(
                (s) =>
                    s === null ||
                    (typeof s === "string" && Array.from(s).length <= 2000 && !s.includes("\0")),
            )
        )
            throw invalid();
        const f = body.logo_file_id,
            v = body.logo_file_version;
        if (f === null) {
            if (v !== null || logo !== null) throw invalid();
        } else if (
            !id(f) ||
            !version(v) ||
            ((previous?.logo_file_id !== f || previous?.logo_file_version !== v) && !logo) ||
            (logo &&
                (logo.id !== f ||
                    logo.ledger_id !== ledgerId ||
                    logo.version !== v ||
                    logo.archived !== false ||
                    !["image/png", "image/jpeg", "image/webp"].includes(logo.detected_media_type)))
        )
            throw invalid();
    }
    return Object.freeze({
        ownerId,
        ledgerId,
        id: identifier,
        action,
        bodyJson: JSON.stringify(body),
        previousJson: previous ? JSON.stringify(previous) : null,
    });
}
export function matchesProfile(row: InvoiceProfile, plan: FrozenProfile): boolean {
    const body = JSON.parse(plan.bodyJson);
    if (row.version !== (plan.action === "create" ? 1 : body.expected_version + 1)) return false;
    const prev = plan.previousJson === null ? null : JSON.parse(plan.previousJson);
    const expected = plan.action === "archive" ? prev : body;
    return (
        row.archived === (plan.action === "archive" ? body.archived : (prev?.archived ?? false)) &&
        row.name === stripCommandText(expected.name) &&
        row.numbering.prefix === expected.numbering.prefix &&
        row.numbering.minimum_digits === expected.numbering.minimum_digits &&
        row.issuer_extra_text === expected.issuer_extra_text &&
        row.payment_instructions === expected.payment_instructions &&
        row.footer === expected.footer &&
        row.logo_file_id === expected.logo_file_id &&
        row.logo_file_version === expected.logo_file_version
    );
}
export const saveProfile = (csrf: string, p: FrozenProfile): Promise<InvoiceProfile> => {
    const base = `/api/v1/ledgers/${encodeURIComponent(p.ledgerId)}/invoice-profiles`;
    const path =
        p.action === "create"
            ? base
            : base + "/" + encodeURIComponent(p.id) + (p.action === "archive" ? "/archive" : "");
    return writeJson(
        path,
        csrf,
        JSON.parse(p.bodyJson),
        p.action === "create" ? "POST" : p.action === "replace" ? "PUT" : "PATCH",
    );
};
