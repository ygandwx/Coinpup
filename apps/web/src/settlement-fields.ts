import type { Account, Asset, Category } from "./ledger-api";
import type { Party } from "./business-api";
import type { BusinessPaymentCreate, InitialAllocationCreate } from "./business-settlement-api";
import { validDraftDate } from "./draft-fields";
import { parseAmount } from "./money";
import { validSettlementInput } from "./settlement-intent";

export type PaymentReferences = {
    accounts: Account[];
    assets: Asset[];
    categories: Category[];
    parties: Party[];
};
export type SettlementFieldError =
    "reference" | "amount" | "date" | "source" | "capacity" | "intent";
export type AllocationSource = Readonly<{
    document_id: string;
    document_line_id: string;
    ledger_id: string;
    party_id: string;
    document_kind: "invoice" | "bill";
    issue_date: string;
    asset_id: string;
    outstanding_amount: string;
    description: string;
    project_id: string | null;
}>;
export type AllocationPayment = Readonly<{
    ledger_id: string;
    party_id: string;
    direction: "receipt" | "payout";
    asset_id: string;
    transaction_date: string;
    unapplied_amount: string;
}>;

/** Current selectable references apply to a new cash movement, never to original-request replay. */
export function paymentFieldError(
    ledgerId: string,
    body: BusinessPaymentCreate,
    refs: PaymentReferences,
): SettlementFieldError | null {
    if (!validDraftDate(body.transaction_date)) return "date";
    if (!validSettlementInput(ledgerId, { kind: "payment", body })) return "intent";
    const party = refs.parties.find(
        (row) => row.id === body.party_id && row.ledger_id === ledgerId,
    );
    const role = body.direction === "receipt" ? "customer" : "supplier";
    if (!party || party.archived || !party.name || ![role, "both"].includes(party.role ?? ""))
        return "reference";
    const supported = (
        accountId: string,
        assetId: string,
        quantity: string,
    ): SettlementFieldError | null => {
        const account = refs.accounts.find(
            (row) => row.id === accountId && row.ledger_id === ledgerId,
        );
        const asset = refs.assets.find((row) => row.asset_id === assetId);
        if (!account || account.archived || !account.asset_ids.includes(assetId) || !asset?.enabled)
            return "reference";
        try {
            return parseAmount(quantity, asset.scale) > 0n ? null : "amount";
        } catch {
            return "amount";
        }
    };
    const principal = supported(body.account_id, body.asset_id, body.amount);
    if (principal) return principal;
    for (const fee of body.fees ?? []) {
        const category = refs.categories.find(
            (row) => row.id === fee.category_id && row.ledger_id === ledgerId,
        );
        if (!category || category.archived || category.kind !== "expense") return "reference";
        const error = supported(fee.account_id, fee.asset_id, fee.amount);
        if (error) return error;
    }
    return null;
}

/** A stale capacity is only a preview. The server rechecks it atomically under the ledger lock. */
export function allocationFieldError(
    group: InitialAllocationCreate,
    payment: AllocationPayment,
    sources: readonly AllocationSource[],
    assets: readonly Asset[],
): SettlementFieldError | null {
    if (
        !validDraftDate(group.allocation_date) ||
        !validDraftDate(payment.transaction_date) ||
        group.allocation_date < payment.transaction_date
    )
        return "date";
    if (!group.lines.length || group.lines.length > 200 || group.lines.length !== sources.length)
        return "source";
    const paymentAsset = assets.find((row) => row.asset_id === payment.asset_id);
    if (!paymentAsset?.enabled) return "reference";
    const seen = new Set<string>();
    let total = 0n;
    try {
        for (let index = 0; index < group.lines.length; index++) {
            const line = group.lines[index],
                source = sources[index];
            if (
                seen.has(line.document_line_id) ||
                source.document_id !== line.document_id ||
                source.document_line_id !== line.document_line_id ||
                source.ledger_id !== payment.ledger_id ||
                source.party_id !== payment.party_id ||
                source.document_kind !== (payment.direction === "receipt" ? "invoice" : "bill")
            )
                return "source";
            seen.add(line.document_line_id);
            if (!validDraftDate(source.issue_date) || group.allocation_date < source.issue_date)
                return "date";
            const documentAsset = assets.find((row) => row.asset_id === source.asset_id);
            if (!documentAsset?.enabled) return "reference";
            const from = parseAmount(line.payment_amount, paymentAsset.scale);
            const to = parseAmount(line.document_amount, documentAsset.scale);
            if (from <= 0n || to <= 0n || (source.asset_id === payment.asset_id && from !== to))
                return "amount";
            if (to > parseAmount(source.outstanding_amount, documentAsset.scale)) return "capacity";
            total += from;
        }
        return total > parseAmount(payment.unapplied_amount, paymentAsset.scale)
            ? "capacity"
            : null;
    } catch {
        return "amount";
    }
}
