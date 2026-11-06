export const id = (n) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
export const ledger = id(1),
    creditId = id(2),
    documentId = id(3);
export const source = (n) => ({
    line_id: id(100 + n),
    line_no: n,
    operation_id: id(200 + n),
    journal_id: id(300 + n),
});
export const currentRow = (n, position = n) => ({
    line_no: position,
    source_line_id: id(100 + n),
    source_operation_id: id(200 + n),
    operation_id: id(400 + n),
    operation_version: n === 1 ? 2 : 1,
    journal_id: id(500 + n),
    net_amount: n === 1 ? "17.23" : "10.00",
    tax_amount: n === 1 ? "2.77" : "0.00",
    total_amount: n === 1 ? "20.00" : "10.00",
    recognition_date: "2026-11-01",
});
export const capacityRow = (n) => ({
    source_line_id: id(100 + n),
    category_id: id(4),
    project_id: null,
    description: "Fictional source 中文",
    recognition_date: "2026-10-01",
    net_amount: "100.00",
    tax_amount: "10.00",
    total_amount: "110.00",
    credited_net_amount: n === 1 ? "20.00" : n === 2 ? "10.00" : "0.00",
    credited_tax_amount: n === 1 ? "4.00" : "0.00",
    applied_amount: n === 1 ? "50.00" : "0.00",
    remaining_net_amount: n === 1 ? "80.00" : n === 2 ? "90.00" : "100.00",
    remaining_tax_amount: n === 1 ? "6.00" : "10.00",
    available_gross_amount: n === 1 ? "36.00" : n === 2 ? "100.00" : "110.00",
});
export function fixture() {
    const body = {
        expected_version: 3,
        reason: "  Fictional correction 中文  ",
        replacement: {
            credit_date: "2026-11-06",
            reason: " Fictional replacement ",
            lines: [
                {
                    source_line_id: id(103),
                    net_amount: "0",
                    tax_amount: "5.00",
                    recognition_date: "2026-11-04",
                },
                {
                    source_line_id: id(101),
                    net_amount: "20.00",
                    tax_amount: "3.0",
                    recognition_date: "2026-11-03",
                },
            ],
        },
    };
    const review = {
        asset: {
            asset_id: "USD",
            code: "USD",
            kind: "fiat",
            scale: 2,
            enabled: true,
            network: null,
            token_reference: null,
        },
        confirmation: {
            kind: "business_confirmation",
            id: id(5),
            ledger_id: ledger,
            document_id: documentId,
            source_version: 1,
            document_version: 2,
            postings: [1, 2, 3].map(source),
        },
        current: {
            credit_id: creditId,
            ledger_id: ledger,
            document_id: documentId,
            document_kind: "bill",
            party_id: id(6),
            asset_id: "USD",
            version: 3,
            status: "active",
            revision_id: id(7),
            revision_reason: "Fictional previous edit",
            credit_date: "2026-11-05",
            credit_reason: "Fictional current",
            members: [1, 2].map((n) => currentRow(n)),
        },
        capacity: {
            id: documentId,
            ledger_id: ledger,
            document_kind: "bill",
            party_id: id(6),
            asset_id: "USD",
            issue_date: "2026-10-01",
            expected_version: 2,
            lines: [1, 2, 3].map(capacityRow),
        },
    };
    return { action: "correct", body, review };
}
export const plan = (f) => ({
    ownerId: "fictional-owner",
    ledgerId: ledger,
    creditId,
    action: f.action,
    key: "fictional-key",
    bodyJson: JSON.stringify(f.body),
    reviewedJson: JSON.stringify(f.review),
});
export function cancelled() {
    const f = fixture();
    f.action = "cancel";
    delete f.body.replacement;
    delete f.review.capacity;
    f.review.asset.enabled = false;
    return f;
}
