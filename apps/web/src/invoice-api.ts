import { ApiError, readJson, writeJson } from "./api";
import { validInvoicePage, validInvoiceSequence } from "./invoice-issuance-receipt";
import type {
    InvoiceCommand,
    InvoiceComplete,
    InvoiceState,
    InvoiceProfile,
    InvoicePage,
} from "./invoice-issuance-receipt";
import { uuid } from "./credit-intent";

const base = (ledger: string) => `/api/v1/ledgers/${encodeURIComponent(ledger)}`;
const issuances = (ledger: string) => `${base(ledger)}/invoice-issuances`;
export const issueInvoice = (
    csrf: string,
    ledger: string,
    body: InvoiceCommand,
    key: string,
): Promise<InvoiceComplete> => writeJson(issuances(ledger), csrf, body, "POST", key);
export const getInvoiceIssuance = (
    ledger: string,
    id: string,
    signal?: AbortSignal,
): Promise<InvoiceState> => readJson(`${issuances(ledger)}/${encodeURIComponent(id)}`, signal);
export const getInvoiceProfile = (
    ledger: string,
    id: string,
    signal?: AbortSignal,
): Promise<InvoiceProfile> =>
    readJson(`${base(ledger)}/invoice-profiles/${encodeURIComponent(id)}`, signal);
export const listInvoiceProfiles = (
    ledger: string,
    offset = 0,
    signal?: AbortSignal,
): Promise<InvoiceProfile[]> => {
    if (!Number.isInteger(offset) || offset < 0 || offset > 100000)
        throw new ApiError("server", null, "invalid_pagination");
    return readJson(
        `${base(ledger)}/invoice-profiles?include_archived=false&limit=50&offset=${offset}`,
        signal,
    );
};
export async function listInvoiceHistory(
    ledger: string,
    doc: string | null = null,
    before: string | null = null,
    signal?: AbortSignal,
    limit = 50,
): Promise<InvoicePage> {
    if (
        !uuid(ledger) ||
        (doc !== null && !uuid(doc)) ||
        (before !== null && !validInvoiceSequence(before)) ||
        !Number.isInteger(limit) ||
        limit < 1 ||
        limit > 200
    )
        throw new ApiError("server", null, "invalid_pagination");
    const query = new URLSearchParams({ limit: String(limit) });
    if (doc !== null) query.set("document_id", doc);
    if (before !== null) query.set("before", before);
    const page = await readJson<unknown>(`${issuances(ledger)}?${query}`, signal);
    if (!validInvoicePage(page, ledger, doc, before, limit))
        throw new ApiError("server", null, "invalid_invoice_history");
    return page;
}
