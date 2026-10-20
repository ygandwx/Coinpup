import type { RefundCapacity } from "./refund-api";
import type { Account, Asset, Category } from "./ledger-api";
import { validDraftDate } from "./draft-fields";
import { parseAmount } from "./money";
import type { FrozenSettlement } from "./settlement-intent";

export type RefundReview = {
    capacity: RefundCapacity;
    paymentOperationId: string;
    paymentJournalId: string;
    accounts: Pick<Account, "id" | "ledger_id" | "kind" | "archived" | "asset_ids">[];
    assets: Pick<Asset, "asset_id" | "enabled" | "scale">[];
    categories: Pick<Category, "id" | "ledger_id" | "kind" | "archived">[];
    returns: { id: string; ledger_id: string; payment_id: string; return_date: string }[];
};
const record = (value: unknown): value is Record<string, unknown> =>
    value !== null && typeof value === "object" && !Array.isArray(value);
const uuid = (value: unknown): value is string =>
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u.test(value);
const keys = (value: Record<string, unknown>, allowed: string[]) =>
    Object.keys(value).every((key) => allowed.includes(key));
const assetId = (value: unknown): value is string =>
    typeof value === "string" && value.length > 0 && value.length <= 200 && !value.includes("\0");
const quantity = (value: unknown, scale = 18): bigint => {
    if (typeof value !== "string" || !Number.isInteger(scale) || scale < 0 || scale > 18)
        throw new Error("Invalid quantity");
    const units = parseAmount(value, scale);
    if (units < 0n || units >= 10n ** BigInt(20 + scale)) throw new Error("Invalid quantity");
    return units;
};
const positive = (value: unknown) => {
    try {
        return quantity(value) > 0n;
    } catch {
        return false;
    }
};
const feeInput = (fee: unknown): boolean =>
    record(fee) &&
    keys(fee, ["account_id", "asset_id", "amount", "category_id"]) &&
    uuid(fee.account_id) &&
    uuid(fee.category_id) &&
    assetId(fee.asset_id) &&
    positive(fee.amount);

export function validRefundInput(value: unknown): boolean {
    if (
        !record(value) ||
        !keys(value, [
            "id",
            "payment_id",
            "expected_version",
            "account_id",
            "asset_id",
            "amount",
            "payment_amount",
            "transaction_date",
            "reason",
            "allocation_return_ids",
            "fees",
        ]) ||
        !uuid(value.id) ||
        !uuid(value.payment_id) ||
        !uuid(value.account_id) ||
        !assetId(value.asset_id) ||
        !Number.isSafeInteger(value.expected_version) ||
        (value.expected_version as number) < 1 ||
        (value.expected_version as number) > 2147483646 ||
        !positive(value.amount) ||
        !positive(value.payment_amount) ||
        typeof value.transaction_date !== "string" ||
        !validDraftDate(value.transaction_date) ||
        typeof value.reason !== "string" ||
        !value.reason.trim() ||
        value.reason.length > 2000 ||
        value.reason.includes("\0")
    )
        return false;
    const returns = value.allocation_return_ids ?? [],
        fees = value.fees ?? [];
    return (
        Array.isArray(returns) &&
        returns.length <= 200 &&
        returns.every(uuid) &&
        new Set(returns).size === returns.length &&
        Array.isArray(fees) &&
        fees.length <= 20 &&
        fees.every(feeInput) &&
        value.allocation_return_ids !== null &&
        value.fees !== null
    );
}

export function validRefundReview(ledgerId: string, body: unknown, reviewed: unknown): boolean {
    if (
        !validRefundInput(body) ||
        !record(body) ||
        !record(reviewed) ||
        !record(reviewed.capacity) ||
        !uuid(ledgerId) ||
        !uuid(reviewed.paymentOperationId) ||
        !uuid(reviewed.paymentJournalId) ||
        ![reviewed.accounts, reviewed.assets, reviewed.categories, reviewed.returns].every(
            (rows) => Array.isArray(rows) && rows.every(record),
        )
    )
        return false;
    const review = reviewed as unknown as RefundReview,
        capacity = review.capacity;
    if (
        capacity.ledger_id !== ledgerId ||
        capacity.id !== body.payment_id ||
        !uuid(capacity.party_id) ||
        capacity.expected_version !== 1 ||
        capacity.expected_version !== body.expected_version ||
        capacity.source_status !== "active" ||
        !["receipt", "payout"].includes(capacity.payment_direction) ||
        !validDraftDate(capacity.transaction_date) ||
        (body.transaction_date as string) < capacity.transaction_date ||
        new Set(review.assets.map((a) => a.asset_id)).size !== review.assets.length ||
        new Set(review.accounts.map((a) => a.id)).size !== review.accounts.length ||
        new Set(review.categories.map((a) => a.id)).size !== review.categories.length ||
        new Set(review.returns.map((a) => a.id)).size !== review.returns.length
    )
        return false;
    try {
        const scale = (asset: string) => {
            const row = review.assets.find((a) => a.asset_id === asset);
            if (!row || row.enabled !== true) throw new Error("Asset unavailable");
            return row.scale;
        };
        const account = (id: unknown, asset: unknown) =>
            review.accounts.some(
                (a) =>
                    a.id === id &&
                    a.ledger_id === ledgerId &&
                    [
                        "bank",
                        "cash",
                        "wechat",
                        "alipay",
                        "credit_card",
                        "paypal",
                        "wise",
                        "stripe",
                        "crypto",
                    ].includes(a.kind) &&
                    a.archived === false &&
                    Array.isArray(a.asset_ids) &&
                    a.asset_ids.includes(asset as string),
            );
        const originalScale = scale(capacity.asset_id),
            cashScale = scale(body.asset_id as string);
        const total = quantity(capacity.amount, originalScale),
            applied = quantity(capacity.applied_amount, originalScale),
            refunded = quantity(capacity.refunded_amount, originalScale),
            remaining = quantity(capacity.refundable_amount, originalScale),
            principal = quantity(body.payment_amount, originalScale),
            cash = quantity(body.amount, cashScale);
        if (
            total <= 0n ||
            total - applied - refunded !== remaining ||
            principal <= 0n ||
            cash <= 0n ||
            principal > remaining ||
            (body.asset_id === capacity.asset_id && cash !== principal) ||
            !account(body.account_id, body.asset_id)
        )
            return false;
        if (
            !((body.allocation_return_ids as string[] | undefined) ?? []).every((id) => {
                const row = review.returns.find((item) => item.id === id);
                return (
                    row &&
                    row.ledger_id === ledgerId &&
                    row.payment_id === capacity.id &&
                    validDraftDate(row.return_date) &&
                    row.return_date <= (body.transaction_date as string)
                );
            })
        )
            return false;
        return ((body.fees as Record<string, unknown>[] | undefined) ?? []).every(
            (fee) =>
                account(fee.account_id, fee.asset_id) &&
                quantity(fee.amount, scale(fee.asset_id as string)) > 0n &&
                review.categories.some(
                    (c) =>
                        c.id === fee.category_id &&
                        c.ledger_id === ledgerId &&
                        c.kind === "expense" &&
                        c.archived === false,
                ),
        );
    } catch {
        return false;
    }
}

export function validRefundReceipt(value: unknown, plan: FrozenSettlement): boolean {
    try {
        const body = JSON.parse(plan.bodyJson) as Record<string, unknown>,
            review = JSON.parse(plan.reviewedJson) as RefundReview;
        if (
            !validRefundReview(plan.ledgerId, body, review) ||
            !record(value) ||
            !keys(value, [
                "kind",
                "id",
                "ledger_id",
                "payment_id",
                "operation_id",
                "journal_id",
                "account_id",
                "asset_id",
                "amount",
                "payment_asset_id",
                "payment_amount",
                "transaction_date",
                "allocation_return_ids",
                "fees",
            ]) ||
            value.kind !== "business_refund" ||
            value.id !== body.id ||
            value.ledger_id !== plan.ledgerId ||
            value.payment_id !== body.payment_id ||
            value.account_id !== body.account_id ||
            value.asset_id !== body.asset_id ||
            value.payment_asset_id !== review.capacity.asset_id ||
            value.transaction_date !== body.transaction_date ||
            !uuid(value.operation_id) ||
            !uuid(value.journal_id) ||
            value.operation_id === review.paymentOperationId ||
            value.journal_id === review.paymentJournalId ||
            quantity(value.amount) !== quantity(body.amount) ||
            quantity(value.payment_amount) !== quantity(body.payment_amount) ||
            !Array.isArray(value.allocation_return_ids) ||
            !Array.isArray(value.fees)
        )
            return false;
        const returns = (body.allocation_return_ids as string[] | undefined) ?? [],
            fees = (body.fees as Record<string, unknown>[] | undefined) ?? [];
        return (
            value.allocation_return_ids.length === returns.length &&
            value.allocation_return_ids.every((id, i) => id === returns[i]) &&
            value.fees.length === fees.length &&
            value.fees.every(
                (fee, i) =>
                    feeInput(fee) &&
                    record(fee) &&
                    fee.account_id === fees[i].account_id &&
                    fee.category_id === fees[i].category_id &&
                    fee.asset_id === fees[i].asset_id &&
                    quantity(fee.amount) === quantity(fees[i].amount),
            )
        );
    } catch {
        return false;
    }
}
