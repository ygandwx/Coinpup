export const id = (n) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
export function refundRevision(action = "correct") {
    const scope = {
        ledger_id: id(1),
        refund_id: id(2),
        payment_id: id(3),
        operation_id: id(4),
        payment_asset_id: "USD",
    };
    const rule = {
        account_id: id(5),
        asset_id: "EUR",
        amount: "19.97",
        payment_amount: "20.00",
        transaction_date: "2026-11-02",
        reason: "Fictional committed cash",
        allocation_return_ids: [id(6)],
        fees: [],
    };
    const fact = {
        ...rule,
        source_version: 1,
        payment_asset_id: "USD",
        recognition_date: rule.transaction_date,
    };
    const original = {
        id: scope.refund_id,
        ledger_id: scope.ledger_id,
        payment_id: scope.payment_id,
        party_id: id(7),
        payment_direction: "receipt",
        source_version: 1,
        payment_asset_id: "USD",
        reason: rule.reason,
        receipt: {
            id: scope.refund_id,
            ledger_id: scope.ledger_id,
            payment_id: scope.payment_id,
            operation_id: scope.operation_id,
            journal_id: id(8),
            kind: "business_refund",
            ...rule,
            payment_asset_id: "USD",
        },
    };
    delete original.receipt.reason;
    const assets = [
        { asset_id: "USD", enabled: true, scale: 2 },
        { asset_id: "EUR", enabled: true, scale: 2 },
        { asset_id: "BTC", enabled: true, scale: 8 },
    ];
    const review = {
        original,
        assets,
        current: {
            ...scope,
            party_id: id(7),
            payment_direction: "receipt",
            version: 1,
            status: "active",
            revision_id: null,
            revision_reason: null,
            journal_id: id(8),
            fact,
        },
        cash: {
            capacity: {
                ledger_id: id(1),
                id: id(3),
                party_id: id(7),
                asset_id: "USD",
                payment_direction: "receipt",
                transaction_date: "2026-11-01",
                amount: "100.00",
                applied_amount: "60.00",
                refunded_amount: "35.00",
                refundable_amount: "5.00",
                expected_version: 1,
                source_status: "active",
            },
            assets,
            accounts: [
                {
                    id: id(5),
                    ledger_id: id(1),
                    archived: false,
                    kind: "bank",
                    asset_ids: ["EUR", "USD", "BTC"],
                },
            ],
            categories: [{ id: id(9), ledger_id: id(1), archived: false, kind: "expense" }],
            returns: [
                { id: id(6), ledger_id: id(1), payment_id: id(3), return_date: "2026-11-02" },
            ],
            paymentOperationId: id(10),
            paymentJournalId: id(11),
        },
    };
    const fees = ["0.00000003", "0.00000001"].map((amount) => ({
        account_id: id(5),
        asset_id: "BTC",
        category_id: id(9),
        amount,
    }));
    const replacement = {
        ...rule,
        reason: "  Fictional replacement ",
        transaction_date: "2026-11-03",
        amount: "22.22",
        payment_amount: "25.00",
        allocation_return_ids: [],
        fees,
    };
    const input = {
        action,
        refundId: scope.refund_id,
        body: {
            expected_version: 1,
            reason: "\u0085Fictional revision\u001d",
            ...(action === "correct" ? { replacement } : {}),
        },
    };
    const result = {
        kind: "business_refund_revision",
        ...scope,
        revision_id: id(13),
        previous_version: 1,
        version: 2,
        action,
        reason: "Fictional revision",
        previous_journal_id: id(8),
        reversal_journal_id: id(14),
        replacement_journal_id: action === "correct" ? id(15) : null,
        previous: structuredClone(fact),
        replacement:
            action === "correct"
                ? {
                      ...replacement,
                      reason: "Fictional replacement",
                      source_version: 1,
                      payment_asset_id: "USD",
                      recognition_date: replacement.transaction_date,
                  }
                : null,
    };
    delete result.payment_asset_id;
    return { scope, review, input, result };
}
export function frozen(f) {
    return {
        ownerId: id(30),
        ledgerId: f.scope.ledger_id,
        refundId: f.input.refundId,
        action: f.input.action,
        key: "fictional-frozen-key",
        bodyJson: JSON.stringify(f.input.body),
        reviewedJson: JSON.stringify(f.review),
    };
}
