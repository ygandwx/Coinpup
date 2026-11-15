import { amountFromMinorUnits, parseAmount } from "./money";
import type { Asset } from "./ledger-api";
import type { RefundCapacity } from "./refund-api";
import { ApiError } from "./api";
import { getAllocationReturn } from "./allocation-return-api";
import { getBusinessPayment } from "./business-settlement-api";
import type { RefundEditor } from "./BusinessRefundForm";
import type { RefundCreate, RefundCurrent, RefundDetail, RefundCorrection } from "./refund-api";
import type { RefundReview } from "./refund-intent";
import { validDraftDate } from "./draft-fields";
import { stripCommandText } from "./command-text";

export function replacementFromEditor(body: RefundCreate): RefundCorrection["replacement"] {
    return {
        account_id: body.account_id,
        asset_id: body.asset_id,
        amount: body.amount,
        payment_amount: body.payment_amount,
        transaction_date: body.transaction_date,
        reason: body.reason,
        fees: body.fees ?? [],
        allocation_return_ids: body.allocation_return_ids ?? [],
    };
}
export function validRevisionReason(reason: string) {
    const stored = stripCommandText(reason);
    return !!stored && !stored.includes("\0") && [...stored].length <= 1000;
}
export async function prepareRefundEditor(
    original: RefundDetail,
    current: RefundCurrent,
    signal: AbortSignal,
): Promise<RefundEditor> {
    const fail = () => new ApiError("server", 200, "invalid_response"),
        fact = current.fact;
    if (
        current.status !== "active" ||
        !fact ||
        current.refund_id !== original.id ||
        current.ledger_id !== original.ledger_id ||
        current.payment_id !== original.payment_id ||
        current.operation_id !== original.receipt.operation_id
    )
        throw fail();
    const payment = await getBusinessPayment(current.ledger_id, current.payment_id, signal);
    if (
        payment.id !== current.payment_id ||
        payment.ledger_id !== current.ledger_id ||
        payment.receipt.kind !== "business_payment" ||
        payment.receipt.id !== payment.id ||
        payment.receipt.ledger_id !== current.ledger_id ||
        payment.party_id !== current.party_id ||
        payment.direction !== current.payment_direction ||
        payment.asset_id !== current.payment_asset_id
    )
        throw fail();
    const selectedReturns: RefundReview["returns"] = [];
    // Resolve only this version's ordered evidence. Cap simultaneous reads on a 200-ID refund.
    for (let i = 0; i < fact.allocation_return_ids.length; i += 4) {
        signal.throwIfAborted();
        const ids = fact.allocation_return_ids.slice(i, i + 4);
        const rows = await Promise.all(
            ids.map((id) => getAllocationReturn(current.ledger_id, id, signal)),
        );
        rows.forEach((r, index) => {
            if (
                r.id !== ids[index] ||
                r.receipt.id !== r.id ||
                r.receipt.kind !== "business_allocation_return" ||
                r.receipt.ledger_id !== r.ledger_id ||
                r.receipt.payment_id !== r.payment_id ||
                r.receipt.return_date !== r.return_date ||
                r.ledger_id !== current.ledger_id ||
                r.payment_id !== payment.id ||
                !validDraftDate(r.return_date) ||
                r.return_date > fact.transaction_date
            )
                throw fail();
            selectedReturns.push({
                id: r.id,
                ledger_id: r.ledger_id,
                payment_id: r.payment_id,
                return_date: r.return_date,
            });
        });
    }
    return {
        payment,
        capacity: null,
        selectedReturns,
        body: {
            id: current.refund_id,
            payment_id: payment.id,
            expected_version: 1,
            account_id: fact.account_id,
            asset_id: fact.asset_id,
            amount: fact.amount,
            payment_amount: fact.payment_amount,
            transaction_date: fact.transaction_date,
            reason: fact.reason,
            fees: fact.fees.map((f) => ({ ...f })),
            allocation_return_ids: [...fact.allocation_return_ids],
        },
    };
}

export function refundReplacementLimit(
    capacity: RefundCapacity,
    ownPrincipal: string,
    assets: Asset[],
): string | null {
    const asset = assets.find((a) => a.asset_id === capacity.asset_id);
    if (!asset || !asset.enabled) return null;
    try {
        const own = parseAmount(ownPrincipal, asset.scale),
            refunded = parseAmount(capacity.refunded_amount, asset.scale);
        const remaining = parseAmount(capacity.refundable_amount, asset.scale);
        if (own <= 0n || own > refunded || remaining < 0n) return null;
        return amountFromMinorUnits(remaining + own, asset.scale);
    } catch {
        return null;
    }
}
