import { ApiError } from "./api";
import { keys, record, uuid } from "./credit-intent";
import { validDocumentCurrent } from "./document-current-facts";
import type { DocumentCurrent } from "./document-revision-api";
import type { Asset } from "./ledger-api";
import { validInvoicePage, validInvoicePolicy } from "./invoice-issuance-receipt";
import type { InvoiceCommand, InvoicePage, InvoiceProfile } from "./invoice-issuance-receipt";

export type InvoiceReview = {
    current: DocumentCurrent;
    asset: Asset;
    profile: InvoiceProfile;
    latest: InvoicePage;
};
export type FrozenInvoice = Readonly<{
    ownerId: string;
    ledgerId: string;
    key: string;
    bodyJson: string;
    reviewedJson: string;
}>;
const version = (v: unknown): v is number =>
    typeof v === "number" && Number.isInteger(v) && v > 0 && v <= 2147483647;
const safeId = (v: unknown): v is string => uuid(v) && v.length === 36;
export function validReviewedInvoiceProfile(v: unknown, ledger: string): v is InvoiceProfile {
    return (
        record(v) &&
        keys(v, [
            "id",
            "ledger_id",
            "version",
            "name",
            "archived",
            "created_at",
            "updated_at",
            "numbering",
            "issuer_extra_text",
            "payment_instructions",
            "footer",
            "logo_file_id",
            "logo_file_version",
        ]) &&
        safeId(v.id) &&
        v.ledger_id === ledger &&
        version(v.version) &&
        typeof v.archived === "boolean" &&
        typeof v.name === "string" &&
        Array.from(v.name).length >= 1 &&
        Array.from(v.name).length <= 160 &&
        !v.name.includes("\0") &&
        typeof v.created_at === "string" &&
        !!v.created_at &&
        typeof v.updated_at === "string" &&
        !!v.updated_at &&
        validInvoicePolicy(v.numbering) &&
        [v.issuer_extra_text, v.payment_instructions, v.footer].every(
            (x) =>
                x === null ||
                (typeof x === "string" && !x.includes("\0") && Array.from(x).length <= 2000),
        ) &&
        (v.logo_file_id === null
            ? v.logo_file_version === null
            : safeId(v.logo_file_id) && version(v.logo_file_version))
    );
}
// Require the latest row for this single document, queried with limit=1, plus the full
// reviewed document (including zero lines), priced asset and current configuration.
export function freezeInvoice(
    ownerId: string,
    ledgerId: string,
    review: InvoiceReview,
): FrozenInvoice {
    const invalid = () => new ApiError("server", null, "invalid_invoice_intent");
    if (
        !safeId(ownerId) ||
        !safeId(ledgerId) ||
        !record(review) ||
        !keys(review, ["current", "asset", "profile", "latest"])
    )
        throw invalid();
    const { current, asset, profile, latest } = review;
    if (
        !record(current) ||
        !safeId(current.document_id) ||
        !validDocumentCurrent(current, ledgerId, current.document_id, asset) ||
        current.document_kind !== "invoice" ||
        current.status !== "active" ||
        !validReviewedInvoiceProfile(profile, ledgerId) ||
        profile.archived ||
        !validInvoicePage(latest, ledgerId, current.document_id, null, 1)
    )
        throw invalid();
    const previous = latest.items[0];
    if (
        previous &&
        (previous.document_status !== "superseded" ||
            previous.current_document_version !== current.version ||
            previous.document_version >= current.version)
    )
        throw invalid();
    const command: InvoiceCommand = {
        id: crypto.randomUUID(),
        document_id: current.document_id,
        expected_document_version: current.version,
        configuration_id: profile.id,
        expected_configuration_version: profile.version,
        previous_issuance_id: previous?.id ?? null,
    };
    return Object.freeze({
        ownerId,
        ledgerId,
        key: crypto.randomUUID(),
        bodyJson: JSON.stringify(command),
        reviewedJson: JSON.stringify(review),
    });
}
