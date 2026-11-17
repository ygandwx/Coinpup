import type { AllocationReturnCapacity, AllocationReturnDetail } from "./allocation-return-api";
import type {
    ReturnCurrent,
    ReturnCorrection,
    ReturnCancellation,
} from "./allocation-return-revision-api";
import { validReturnCurrent } from "./allocation-return-revision-facts";
import { stripCommandText } from "./command-text";
import { parseAmount } from "./money";
import { assetId, keys, record, uuid } from "./refund-intent";
import type { RefundReview } from "./refund-intent";
import { date, reason, storedUnits, version } from "./settlement-revision-facts";

export type ReturnRevisionInput =
    | { action: "correct"; returnId: string; body: ReturnCorrection }
    | { action: "cancel"; returnId: string; body: ReturnCancellation };
export type ReturnRevisionReview = {
    original: AllocationReturnDetail;
    current: ReturnCurrent;
    assets: RefundReview["assets"];
    capacities: AllocationReturnCapacity[];
};
type Source = AllocationReturnCapacity["lines"][number];
type Member = ReturnCurrent["members"][number];

export function returnScope(original: AllocationReturnDetail) {
    return {
        ledger_id: original.ledger_id,
        return_id: original.id,
        payment_id: original.payment_id,
        payment_asset_id: original.asset_id,
    };
}

export function reviewedSources(capacities: AllocationReturnCapacity[]) {
    const byId = new Map<
        string,
        { source: Source; allocationId: string; allocationDate: string }
    >();
    for (const cap of capacities) {
        for (const source of cap.lines) {
            if (byId.has(source.source_allocation_line_id))
                throw new Error("Duplicate reviewed source");
            byId.set(source.source_allocation_line_id, {
                source,
                allocationId: cap.id,
                allocationDate: cap.allocation_date,
            });
        }
    }
    return byId;
}
const commandReason = (v: unknown, max: number) =>
    typeof v === "string" && [...v].length <= max && reason(stripCommandText(v), max);

export function validReturnRevisionInput(input: unknown): input is ReturnRevisionInput {
    if (
        !record(input) ||
        !keys(input, ["action", "returnId", "body"]) ||
        !uuid(input.returnId) ||
        !record(input.body)
    )
        return false;
    const b = input.body;
    if (
        !version(b.expected_version) ||
        b.expected_version >= 2147483647 ||
        !commandReason(b.reason, 1000)
    )
        return false;
    if (input.action === "cancel") return keys(b, ["expected_version", "reason"]);
    const r = b.replacement;
    if (
        input.action !== "correct" ||
        !keys(b, ["expected_version", "reason", "replacement"]) ||
        !record(r) ||
        !keys(r, ["reason", "return_date", "lines"]) ||
        !commandReason(r.reason, 2000) ||
        !date(r.return_date) ||
        !Array.isArray(r.lines) ||
        r.lines.length < 1 ||
        r.lines.length > 200
    )
        return false;
    const ids = new Set<string>();
    return r.lines.every((line) => {
        if (
            !record(line) ||
            !keys(line, [
                "source_allocation_line_id",
                "expected_version",
                "payment_amount",
                "document_amount",
            ]) ||
            !uuid(line.source_allocation_line_id) ||
            ids.has(line.source_allocation_line_id) ||
            line.expected_version !== 1 ||
            typeof line.payment_amount !== "string" ||
            typeof line.document_amount !== "string"
        )
            return false;
        ids.add(line.source_allocation_line_id);
        try {
            return (
                parseAmount(line.payment_amount, 18) > 0n &&
                parseAmount(line.document_amount, 18) > 0n
            );
        } catch {
            return false;
        }
    });
}

type SourceIdentity = Pick<
    Source,
    "source_allocation_line_id" | "source_operation_id" | "document_id" | "document_line_id"
>;
export function sameSource(a: SourceIdentity, b: SourceIdentity) {
    return (
        a.source_allocation_line_id === b.source_allocation_line_id &&
        a.source_operation_id === b.source_operation_id &&
        a.document_id === b.document_id &&
        a.document_line_id === b.document_line_id
    );
}
type AmountPair = { payment_amount: string; document_amount: string };
export function sameReturnAmounts(
    a: AmountPair,
    b: AmountPair,
    payment: string,
    document: string,
    assets: ReturnRevisionReview["assets"],
) {
    return (
        storedUnits(a.payment_amount, payment, assets) ===
            storedUnits(b.payment_amount, payment, assets) &&
        storedUnits(a.document_amount, document, assets) ===
            storedUnits(b.document_amount, document, assets)
    );
}

function validOriginal(r: ReturnRevisionReview) {
    const o = r.original,
        c = r.current,
        receipt = o.receipt;
    if (
        receipt.kind !== "business_allocation_return" ||
        receipt.id !== o.id ||
        receipt.ledger_id !== o.ledger_id ||
        receipt.payment_id !== o.payment_id ||
        receipt.return_date !== o.return_date ||
        !date(o.return_date) ||
        !Array.isArray(o.lines) ||
        !Array.isArray(receipt.postings) ||
        o.lines.length !== o.line_count ||
        o.line_count < 1 ||
        o.line_count > 200 ||
        receipt.postings.length !== o.line_count
    )
        return false;
    if (
        c.version === 1 &&
        (c.fact!.return_date !== o.return_date ||
            c.fact!.reason !== o.reason ||
            c.members.length !== o.line_count)
    )
        return false;
    const postings = receipt.postings;
    for (const field of [
        "source_allocation_line_id",
        "source_operation_id",
        "operation_id",
        "journal_id",
    ] as const)
        if (new Set(postings.map((row) => row[field])).size !== postings.length) return false;
    const operations = new Set(postings.map((row) => row.operation_id));
    if (postings.some((row) => operations.has(row.source_operation_id))) return false;
    const total = postings.reduce(
        (sum, row) => sum + storedUnits(row.payment_amount, o.asset_id, r.assets),
        0n,
    );
    if (storedUnits(o.payment_amount, o.asset_id, r.assets) !== total) return false;
    return o.lines.every((line, i) => {
        const post = receipt.postings[i],
            m = c.members[i];
        if (
            !post ||
            post.line_no !== i + 1 ||
            line.line_no !== post.line_no ||
            !uuid(line.source_allocation_id) ||
            !uuid(post.operation_id) ||
            !uuid(post.journal_id) ||
            !uuid(post.source_allocation_line_id) ||
            !uuid(post.source_operation_id) ||
            !uuid(post.document_id) ||
            !uuid(post.document_line_id) ||
            post.operation_id === post.source_operation_id ||
            line.operation_id !== post.operation_id ||
            line.journal_id !== post.journal_id ||
            !sameSource(line, post) ||
            !sameReturnAmounts(line, post, o.asset_id, line.document_asset_id, r.assets)
        )
            return false;
        return (
            c.version !== 1 ||
            (m.operation_id === post.operation_id &&
                m.journal_id === post.journal_id &&
                sameSource(m, post) &&
                m.document_asset_id === line.document_asset_id &&
                sameReturnAmounts(m, post, o.asset_id, line.document_asset_id, r.assets))
        );
    });
}
function withinCapacity(
    input: string,
    total: string,
    returned: string,
    remaining: string,
    old: string | undefined,
    asset: string,
    review: ReturnRevisionReview,
) {
    const a = review.assets.find((a) => a.asset_id === asset && a.enabled);
    if (!a) return false;
    const requested = parseAmount(input, a.scale),
        orig = parseAmount(total, a.scale),
        used = parseAmount(returned, a.scale),
        free = parseAmount(remaining, a.scale);
    const previous = old === undefined ? 0n : storedUnits(old, asset, review.assets);
    return (
        orig > 0n &&
        used >= previous &&
        free >= 0n &&
        used + free === orig &&
        requested > 0n &&
        requested <= free + previous
    );
}
function validReplacement(r: ReturnRevisionReview, replacement: ReturnCorrection["replacement"]) {
    const c = r.current,
        old = new Map(c.members.map((m) => [m.source_allocation_line_id, m]));
    if (
        !r.capacities.every(
            (cap) =>
                uuid(cap.id) &&
                cap.ledger_id === c.ledger_id &&
                cap.payment_id === c.payment_id &&
                cap.asset_id === c.payment_asset_id &&
                date(cap.allocation_date) &&
                Array.isArray(cap.lines) &&
                cap.lines.length > 0 &&
                cap.lines.length <= 200,
        )
    )
        return false;
    const sources = reviewedSources(r.capacities);
    const originalLines = new Map(
        r.original.lines.map((line) => [line.source_allocation_line_id, line]),
    );
    return replacement.lines.every((line) => {
        const item = sources.get(line.source_allocation_line_id);
        if (!item || item.allocationDate > replacement.return_date) return false;
        const s = item.source,
            previous: Member | undefined = old.get(line.source_allocation_line_id);
        const original = originalLines.get(line.source_allocation_line_id);
        if (
            s.expected_version !== 1 ||
            s.source_status !== "active" ||
            !uuid(s.source_operation_id) ||
            !uuid(s.document_id) ||
            !uuid(s.document_line_id) ||
            (original &&
                (original.source_allocation_id !== item.allocationId ||
                    !sameSource(original, s) ||
                    original.document_asset_id !== s.document_asset_id)) ||
            (previous &&
                (!sameSource(previous, s) || previous.document_asset_id !== s.document_asset_id))
        )
            return false;
        // A retained source can reuse its own old amounts. The server checks its original
        // slot, every later rounded installment, and competing refunds/credits under lock.
        return (
            withinCapacity(
                line.payment_amount,
                s.payment_amount,
                s.returned_payment_amount,
                s.remaining_payment_amount,
                previous?.payment_amount,
                c.payment_asset_id,
                r,
            ) &&
            withinCapacity(
                line.document_amount,
                s.document_amount,
                s.returned_document_amount,
                s.remaining_document_amount,
                previous?.document_amount,
                s.document_asset_id,
                r,
            ) &&
            (c.payment_asset_id !== s.document_asset_id ||
                parseAmount(line.payment_amount, 18) === parseAmount(line.document_amount, 18))
        );
    });
}
export function validReturnRevisionReview(
    ledgerId: string,
    input: ReturnRevisionInput,
    review: unknown,
): review is ReturnRevisionReview {
    if (
        !validReturnRevisionInput(input) ||
        !record(review) ||
        !record(review.original) ||
        !record(review.original.receipt) ||
        !Array.isArray(review.assets) ||
        !Array.isArray(review.capacities) ||
        !review.assets.every(
            (a) =>
                record(a) &&
                assetId(a.asset_id) &&
                typeof a.enabled === "boolean" &&
                Number.isInteger(a.scale) &&
                (a.scale as number) >= 0 &&
                (a.scale as number) <= 18,
        ) ||
        new Set(review.assets.map((a) => a.asset_id)).size !== review.assets.length
    )
        return false;
    const r = review as unknown as ReturnRevisionReview;
    try {
        return (
            r.original.ledger_id === ledgerId &&
            r.original.id === input.returnId &&
            validReturnCurrent(r.current, returnScope(r.original), r.assets) &&
            r.current.status === "active" &&
            r.current.version === input.body.expected_version &&
            validOriginal(r) &&
            (input.action === "cancel" || validReplacement(r, input.body.replacement))
        );
    } catch {
        return false;
    }
}
