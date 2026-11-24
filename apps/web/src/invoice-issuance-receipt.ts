import { keys, record, uuid as knownUuid } from "./credit-intent";
import type { components } from "./generated/openapi";

export type InvoiceCommand = components["schemas"]["InvoiceIssuanceCreate"];
export type InvoicePending = components["schemas"]["InvoiceIssuancePending"];
export type InvoiceComplete = components["schemas"]["InvoiceIssuanceComplete"];
export type InvoiceState = InvoicePending | InvoiceComplete;
export type InvoiceArtifact = components["schemas"]["InvoicePdfArtifact"];
export type InvoicePolicy = components["schemas"]["InvoiceNumberingPolicy"];
export type InvoiceProfile = components["schemas"]["InvoiceProfileResponse"];
export type InvoiceRow = components["schemas"]["InvoiceHistoryItem"];
export type InvoicePage = components["schemas"]["InvoiceHistoryPage"];

const maximum = 9223372036854775807n;
const identity = [
    "id",
    "ledger_id",
    "document_id",
    "document_version",
    "configuration_id",
    "configuration_version",
    "previous_issuance_id",
    "kind",
    "numbering",
    "number",
    "sequence",
    "snapshot_sha256",
    "template_version",
    "state",
    "artifacts",
];
const uuid = (v: unknown): v is string => knownUuid(v) && v.length === 36;
const sameUuid = (a: unknown, b: string) =>
    typeof a === "string" && uuid(a) && typeof b === "string" && a === b.toLowerCase();
const v32 = (n: unknown, minimum = 1): n is number =>
    typeof n === "number" && Number.isInteger(n) && n >= minimum && n <= 2147483647;
const digest = (v: unknown): v is string =>
    typeof v === "string" && v.length === 64 && !/[^0-9a-f]/u.test(v);
export const validInvoiceSequence = (v: unknown): v is string =>
    typeof v === "string" &&
    v.length <= 19 &&
    v.length > 0 &&
    v[0] !== "0" &&
    !/[^0-9]/u.test(v) &&
    BigInt(v) <= maximum;
const text = (v: unknown, max: number): v is string =>
    typeof v === "string" &&
    Array.from(v).length <= max &&
    v.trim() === v &&
    !/[\p{C}]/u.test(v) &&
    !/[^\S ]/u.test(v);
export const validInvoicePolicy = (v: unknown): v is InvoicePolicy =>
    record(v) &&
    keys(v, ["prefix", "minimum_digits"]) &&
    text(v.prefix, 40) &&
    v32(v.minimum_digits) &&
    v.minimum_digits <= 20;
const artifact = (v: unknown, language: "zh" | "en"): v is InvoiceArtifact =>
    record(v) &&
    keys(v, ["language", "file_id", "sha256", "byte_size", "media_type"]) &&
    v.language === language &&
    uuid(v.file_id) &&
    digest(v.sha256) &&
    v32(v.byte_size) &&
    v.byte_size <= 8 * 1024 * 1024 &&
    v.media_type === "application/pdf";

export function validInvoiceState(
    value: unknown,
    ledgerId: string,
    command: InvoiceCommand,
    reviewedPolicy: InvoicePolicy,
    prior?: InvoicePending,
): value is InvoiceState {
    if (
        !record(value) ||
        !keys(value, identity) ||
        !sameUuid(value.id, command.id) ||
        !sameUuid(value.ledger_id, ledgerId) ||
        !sameUuid(value.document_id, command.document_id) ||
        !sameUuid(value.configuration_id, command.configuration_id) ||
        !v32(value.document_version, 2) ||
        value.document_version !== command.expected_document_version ||
        !v32(value.configuration_version) ||
        value.configuration_version !== command.expected_configuration_version ||
        (command.previous_issuance_id === null
            ? value.previous_issuance_id !== null
            : !sameUuid(value.previous_issuance_id, command.previous_issuance_id)) ||
        value.previous_issuance_id === value.id ||
        value.kind !== "invoice_issuance" ||
        value.template_version !== "commercial-invoice-v1" ||
        !digest(value.snapshot_sha256) ||
        !validInvoiceSequence(value.sequence) ||
        !validInvoicePolicy(value.numbering) ||
        !validInvoicePolicy(reviewedPolicy) ||
        value.numbering.prefix !== reviewedPolicy.prefix ||
        value.numbering.minimum_digits !== reviewedPolicy.minimum_digits ||
        value.number !==
            reviewedPolicy.prefix + value.sequence.padStart(reviewedPolicy.minimum_digits, "0") ||
        !Array.isArray(value.artifacts)
    )
        return false;
    if (
        prior &&
        (!validInvoiceState(prior, ledgerId, command, reviewedPolicy) ||
            prior.state !== "pending" ||
            value.number !== prior.number ||
            value.sequence !== prior.sequence ||
            value.snapshot_sha256 !== prior.snapshot_sha256)
    )
        return false;
    if (value.state === "pending") return value.artifacts.length === 0;
    if (value.state !== "complete" || value.artifacts.length !== 2) return false;
    const [zh, en] = value.artifacts;
    return (
        artifact(zh, "zh") &&
        artifact(en, "en") &&
        zh.file_id !== en.file_id &&
        zh.sha256 !== en.sha256
    );
}

export function validInvoicePage(
    page: unknown,
    ledgerId: string,
    documentId: string | null,
    before: string | null,
    limit = 50,
): page is InvoicePage {
    if (
        !record(page) ||
        !keys(page, ["items", "next_before"]) ||
        !Array.isArray(page.items) ||
        !Number.isInteger(limit) ||
        limit < 1 ||
        limit > 200 ||
        page.items.length > limit ||
        (before !== null && !validInvoiceSequence(before))
    )
        return false;
    let last = before === null ? maximum + 1n : BigInt(before);
    const seen = new Set<string>();
    for (const r of page.items) {
        if (
            !record(r) ||
            !keys(r, [
                "id",
                "ledger_id",
                "document_id",
                "document_version",
                "previous_issuance_id",
                "sequence",
                "number",
                "state",
                "current_document_version",
                "document_status",
            ]) ||
            !uuid(r.id) ||
            seen.has(r.id) ||
            !sameUuid(r.ledger_id, ledgerId) ||
            !uuid(r.document_id) ||
            (documentId !== null && !sameUuid(r.document_id, documentId)) ||
            (r.previous_issuance_id !== null && !uuid(r.previous_issuance_id)) ||
            r.previous_issuance_id === r.id ||
            !v32(r.document_version, 2) ||
            !v32(r.current_document_version, 2) ||
            r.document_version > r.current_document_version ||
            !validInvoiceSequence(r.sequence) ||
            BigInt(r.sequence) >= last ||
            !text(r.number, 60) ||
            r.number.length === 0 ||
            (r.state !== "pending" && r.state !== "complete") ||
            (r.document_status !== "current" &&
                r.document_status !== "superseded" &&
                r.document_status !== "cancelled") ||
            (r.document_status === "current") !==
                (r.document_version === r.current_document_version)
        )
            return false;
        seen.add(r.id);
        last = BigInt(r.sequence);
    }
    return (
        page.next_before === null ||
        (page.items.length === limit &&
            validInvoiceSequence(page.next_before) &&
            BigInt(page.next_before) === last)
    );
}

// A selected historical row has no profile or artifact data. Bind its detail to that
// exact row; the current editable numbering profile is never historical evidence.
export function validListedInvoice(value: unknown, row: InvoiceRow): value is InvoiceState {
    if (
        !record(value) ||
        !uuid(value.configuration_id) ||
        !v32(value.configuration_version) ||
        !validInvoicePolicy(value.numbering)
    )
        return false;
    const command: InvoiceCommand = {
        id: row.id,
        document_id: row.document_id,
        expected_document_version: row.document_version,
        configuration_id: value.configuration_id,
        expected_configuration_version: value.configuration_version,
        previous_issuance_id: row.previous_issuance_id,
    };
    return (
        validInvoiceState(value, row.ledger_id, command, value.numbering) &&
        value.sequence === row.sequence &&
        value.number === row.number &&
        (row.state === "pending" || value.state === "complete")
    );
}
