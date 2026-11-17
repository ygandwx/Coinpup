import { registerHooks } from "node:module";
registerHooks({
    resolve(specifier, context, next) {
        return next(
            specifier.startsWith(".") &&
                !/\.[a-z]+$/u.test(specifier) &&
                context.parentURL?.includes("/src/")
                ? specifier + ".ts"
                : specifier,
            context,
        );
    },
});
export const id = (n) => "20000000-0000-4000-9000-" + String(n).padStart(12, "0");
export function setupReturn(count = 1) {
    const ledger = id(1),
        group = id(2),
        payment = id(3),
        allocation = id(7);
    const assets = [
        { asset_id: "USD", scale: 2, enabled: true },
        { asset_id: "BTC", scale: 8, enabled: true },
    ];
    const fact = { return_date: "2026-01-02", reason: "Fictional original 中文" };
    const sources = Array.from({ length: count * 2 }, (_, i) => ({
        source_allocation_line_id: id(1000 + i),
        source_operation_id: id(2000 + i),
        document_id: id(4),
        document_line_id: id(3000 + i),
        document_asset_id: "BTC",
        source_status: "active",
        expected_version: 1,
        description: "Fictional line",
        payment_amount: "0.12",
        document_amount: "0.00000012",
        returned_payment_amount: i < count ? "0.08" : "0.00",
        returned_document_amount: i < count ? "0.00000008" : "0.00000000",
        remaining_payment_amount: i < count ? "0.04" : "0.12",
        remaining_document_amount: i < count ? "0.00000004" : "0.00000012",
    }));
    const postings = sources.slice(0, count).map((s, i) => ({
        line_no: i + 1,
        source_allocation_line_id: s.source_allocation_line_id,
        source_operation_id: s.source_operation_id,
        document_id: s.document_id,
        document_line_id: s.document_line_id,
        operation_id: id(4000 + i),
        journal_id: id(5000 + i),
        payment_amount: "0.050000000000000000",
        document_amount: "0.000000050000000000",
    }));
    const current = {
        ledger_id: ledger,
        return_id: group,
        payment_id: payment,
        payment_asset_id: "USD",
        party_id: id(5),
        payment_direction: "receipt",
        version: 1,
        revision_id: null,
        revision_reason: null,
        status: "active",
        fact,
        members: postings.map((p, i) => ({
            ...p,
            source_version: 1,
            source_return_no: "9007199254740993",
            operation_version: 1,
            document_asset_id: "BTC",
            project_id: null,
            line_no: i + 1,
        })),
    };
    const capacity = (chunk, i) => ({
        ledger_id: ledger,
        payment_id: payment,
        id: id(7 + i),
        asset_id: "USD",
        allocation_date: "2026-01-01",
        lines: chunk,
    });
    const original = {
        ledger_id: ledger,
        id: group,
        payment_id: payment,
        asset_id: "USD",
        ...fact,
        payment_amount:
            (BigInt(count) / 20n).toString() +
            "." +
            ((BigInt(count) * 5n) % 100n).toString().padStart(2, "0"),
        line_count: count,
        created_at: "2026-01-02T00:00:00Z",
        receipt: {
            ledger_id: ledger,
            id: group,
            payment_id: payment,
            return_date: fact.return_date,
            kind: "business_allocation_return",
            postings,
        },
        lines: postings.map((p) => ({
            ...p,
            source_allocation_id: allocation,
            description: "Fictional",
            document_asset_id: "BTC",
        })),
    };
    const review = {
        original,
        current,
        assets,
        capacities: [capacity(sources.slice(0, count), 0), capacity(sources.slice(count), 1)],
    };
    const line = (s) => ({
        source_allocation_line_id: s.source_allocation_line_id,
        expected_version: 1,
        payment_amount: "0.09",
        document_amount: "0.00000009",
    });
    const correct = {
        action: "correct",
        returnId: group,
        body: {
            expected_version: 1,
            reason: "  Fictional revision 中文 ",
            replacement: {
                return_date: "2026-01-03",
                reason: " Fictional replacement ",
                lines: sources.slice(0, count).map(line),
            },
        },
    };
    const cancel = {
        action: "cancel",
        returnId: group,
        body: { expected_version: 1, reason: " Fictional cancellation " },
    };
    return { ledger, group, payment, review, correct, cancel, line, sources };
}
