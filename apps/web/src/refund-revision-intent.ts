import type {
    RefundCancellation,
    RefundCorrection,
    RefundCurrent,
    RefundDetail,
    RefundFact,
    RefundRevisionReceipt,
} from "./refund-api";
import type { RefundReview } from "./refund-intent";
import { keys, record, uuid, validRefundCashFields, validRefundCashReview } from "./refund-intent";
import {
    storedRefundUnits,
    validRefundCurrent,
    validRefundRevisionDetail,
} from "./refund-revision-read";
import type { RefundScope } from "./refund-revision-read";
import { stripCommandText } from "./command-text";

export type RefundRevisionInput =
    | { action: "correct"; refundId: string; body: RefundCorrection }
    | { action: "cancel"; refundId: string; body: RefundCancellation };
export type RefundRevisionReview = {
    current: RefundCurrent;
    original: RefundDetail;
    assets: RefundReview["assets"];
    cash?: RefundReview;
};
export type FrozenRefundRevision = Readonly<{
    ownerId: string;
    ledgerId: string;
    refundId: string;
    action: "correct" | "cancel";
    key: string;
    bodyJson: string;
    reviewedJson: string;
}>;

export function validRefundRevisionInput(value: unknown): value is RefundRevisionInput {
    if (
        !record(value) ||
        !keys(value, ["action", "refundId", "body"]) ||
        !uuid(value.refundId) ||
        !record(value.body)
    )
        return false;
    const body = value.body;
    if (
        !Number.isSafeInteger(body.expected_version) ||
        (body.expected_version as number) < 1 ||
        (body.expected_version as number) > 2147483646 ||
        typeof body.reason !== "string" ||
        body.reason.includes("\0") ||
        !stripCommandText(body.reason) ||
        [...stripCommandText(body.reason)].length > 1000
    )
        return false;
    if (value.action === "cancel") return keys(body, ["expected_version", "reason"]);
    const replacement = body.replacement;
    return (
        value.action === "correct" &&
        keys(body, ["expected_version", "reason", "replacement"]) &&
        record(replacement) &&
        keys(replacement, [
            "account_id",
            "asset_id",
            "amount",
            "payment_amount",
            "transaction_date",
            "reason",
            "fees",
            "allocation_return_ids",
        ]) &&
        typeof replacement.reason === "string" &&
        !replacement.reason.includes("\0") &&
        !!stripCommandText(replacement.reason) &&
        [...replacement.reason].length <= 2000 &&
        validRefundCashFields(replacement)
    );
}

function scope(ledgerId: string, refundId: string, original: RefundDetail): RefundScope {
    return {
        ledger_id: ledgerId,
        refund_id: refundId,
        payment_id: original.payment_id,
        operation_id: original.receipt.operation_id,
        payment_asset_id: original.payment_asset_id,
    };
}

export function validRefundRevisionReview(
    ledgerId: string,
    input: RefundRevisionInput,
    reviewed: unknown,
): reviewed is RefundRevisionReview {
    if (
        !validRefundRevisionInput(input) ||
        !record(reviewed) ||
        !record(reviewed.original) ||
        !record(reviewed.original.receipt) ||
        !Array.isArray(reviewed.assets) ||
        !reviewed.assets.every(
            (a) =>
                record(a) &&
                typeof a.asset_id === "string" &&
                typeof a.enabled === "boolean" &&
                Number.isInteger(a.scale) &&
                (a.scale as number) >= 0 &&
                (a.scale as number) <= 18,
        ) ||
        new Set(reviewed.assets.map((a) => a.asset_id)).size !== reviewed.assets.length
    )
        return false;
    const r = reviewed as unknown as RefundRevisionReview,
        o = r.original,
        c = r.current;
    if (
        o.id !== input.refundId ||
        o.ledger_id !== ledgerId ||
        o.receipt.kind !== "business_refund" ||
        o.receipt.id !== o.id ||
        o.receipt.ledger_id !== ledgerId ||
        o.receipt.payment_id !== o.payment_id ||
        o.receipt.payment_asset_id !== o.payment_asset_id ||
        !uuid(o.receipt.journal_id) ||
        o.source_version !== 1 ||
        !validRefundCurrent(
            c,
            scope(ledgerId, input.refundId, o),
            o.party_id,
            o.payment_direction,
            r.assets,
        ) ||
        c.status !== "active" ||
        c.version !== input.body.expected_version ||
        !c.fact ||
        (c.version === 1 && c.journal_id !== o.receipt.journal_id)
    )
        return false;
    if (
        c.version === 1 &&
        !sameFact(
            c.fact,
            {
                account_id: o.receipt.account_id,
                asset_id: o.receipt.asset_id,
                amount: o.receipt.amount,
                payment_asset_id: o.receipt.payment_asset_id,
                payment_amount: o.receipt.payment_amount,
                source_version: 1,
                reason: o.reason,
                transaction_date: o.receipt.transaction_date,
                recognition_date: o.receipt.transaction_date,
                fees: o.receipt.fees,
                allocation_return_ids: o.receipt.allocation_return_ids,
            },
            r.assets,
        )
    )
        return false;
    if (input.action === "cancel") return true;
    const cash = r.cash;
    if (
        !cash ||
        cash.paymentOperationId === c.operation_id ||
        cash.paymentJournalId === o.receipt.journal_id ||
        !record(cash.capacity) ||
        cash.capacity.asset_id !== c.payment_asset_id ||
        cash.capacity.payment_direction !== c.payment_direction ||
        cash.capacity.party_id !== c.party_id ||
        !Array.isArray(cash.assets) ||
        cash.assets.length !== r.assets.length ||
        !cash.assets.every(
            (a) =>
                record(a) &&
                r.assets.some(
                    (b) =>
                        a.asset_id === b.asset_id && a.enabled === b.enabled && a.scale === b.scale,
                ),
        )
    )
        return false;
    try {
        return validRefundCashReview(
            ledgerId,
            { ...input.body.replacement, payment_id: c.payment_id, expected_version: 1 },
            cash,
            storedRefundUnits(c.fact.payment_amount, c.payment_asset_id, r.assets),
        );
    } catch {
        return false;
    }
}

function sameFact(a: RefundFact, b: RefundFact, assets: RefundReview["assets"]): boolean {
    try {
        if (
            a.account_id !== b.account_id ||
            a.asset_id !== b.asset_id ||
            a.payment_asset_id !== b.payment_asset_id ||
            a.source_version !== b.source_version ||
            a.transaction_date !== b.transaction_date ||
            a.recognition_date !== b.recognition_date ||
            a.reason !== b.reason ||
            a.fees.length !== b.fees.length ||
            a.allocation_return_ids.length !== b.allocation_return_ids.length ||
            !a.allocation_return_ids.every((id, i) => id === b.allocation_return_ids[i])
        )
            return false;
        const equal = (x: string, y: string, asset: string) =>
            storedRefundUnits(x, asset, assets) === storedRefundUnits(y, asset, assets);
        return (
            equal(a.amount, b.amount, a.asset_id) &&
            equal(a.payment_amount, b.payment_amount, a.payment_asset_id) &&
            a.fees.every((f, i) => {
                const g = b.fees[i];
                return (
                    f.account_id === g.account_id &&
                    f.category_id === g.category_id &&
                    f.asset_id === g.asset_id &&
                    equal(f.amount, g.amount, f.asset_id)
                );
            })
        );
    } catch {
        return false;
    }
}

export function validFrozenRefundRevision(
    value: unknown,
    plan: FrozenRefundRevision,
): value is RefundRevisionReceipt {
    try {
        const input: RefundRevisionInput = {
            action: plan.action,
            refundId: plan.refundId,
            body: JSON.parse(plan.bodyJson),
        };
        const reviewed: unknown = JSON.parse(plan.reviewedJson);
        if (
            !validRefundRevisionReview(plan.ledgerId, input, reviewed) ||
            !record(value) ||
            !uuid(value.revision_id)
        )
            return false;
        const c = reviewed.current,
            assets = reviewed.assets,
            previous = c.fact!;
        const selected = {
            action: input.action,
            version: c.version + 1,
            reason: stripCommandText(input.body.reason),
            revision_id: value.revision_id,
        };
        if (
            !validRefundRevisionDetail(
                value,
                scope(plan.ledgerId, input.refundId, reviewed.original),
                selected,
                assets,
            ) ||
            value.previous_journal_id !== c.journal_id ||
            value.reversal_journal_id === reviewed.original.receipt.journal_id ||
            value.replacement_journal_id === reviewed.original.receipt.journal_id ||
            value.revision_id === c.revision_id ||
            !sameFact(value.previous, previous, assets)
        )
            return false;
        if (input.action === "cancel") return true;
        const b = input.body.replacement;
        return (
            !!value.replacement &&
            sameFact(
                value.replacement,
                {
                    ...b,
                    reason: stripCommandText(b.reason),
                    source_version: 1,
                    payment_asset_id: c.payment_asset_id,
                    recognition_date: b.transaction_date,
                    fees: b.fees ?? [],
                    allocation_return_ids: b.allocation_return_ids ?? [],
                },
                assets,
            )
        );
    } catch {
        return false;
    }
}
