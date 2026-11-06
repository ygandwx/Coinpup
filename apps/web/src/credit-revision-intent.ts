import type { CreditCancellation, CreditCorrection, CreditCurrent } from "./credit-api";
import {
    date,
    keys,
    quantity,
    record,
    uuid,
    validCreditInput,
    validCreditReview,
} from "./credit-intent";
import type { CreditReview, FrozenCredit } from "./credit-intent";
import { amountFromMinorUnits } from "./money";

export type CreditRevisionAction = "correct" | "cancel";
export type CreditRevisionReview = Omit<CreditReview, "capacity"> & {
    current: CreditCurrent;
    capacity?: CreditReview["capacity"];
};
export type FrozenCreditRevision = FrozenCredit &
    Readonly<{
        action: CreditRevisionAction;
        creditId: string;
    }>;
export function isCreditRevisionPlan(plan: FrozenCredit | null): plan is FrozenCreditRevision {
    return (
        plan !== null &&
        "action" in plan &&
        (plan.action === "correct" || plan.action === "cancel") &&
        "creditId" in plan &&
        typeof plan.creditId === "string"
    );
}
// Python str.strip includes NEL and separators U+001C..001F, but preserves BOM.
export function stripCreditRevisionReason(value: string): string {
    const space = (char: string) =>
        /^\p{White_Space}$/u.test(char) || (char >= "\u001c" && char <= "\u001f");
    let start = 0,
        end = value.length;
    while (start < end && space(value[start])) start += 1;
    while (end > start && space(value[end - 1])) end -= 1;
    return value.slice(start, end);
}
const reason = (v: unknown, max: number, stripFirst = false): v is string =>
    typeof v === "string" &&
    !!stripCreditRevisionReason(v) &&
    !v.includes("\0") &&
    [...(stripFirst ? stripCreditRevisionReason(v) : v)].length <= max;
const version = (v: unknown, max = 2147483647): v is number =>
    Number.isSafeInteger(v) && (v as number) >= 1 && (v as number) <= max;
const distinctIds = (values: unknown[]) =>
    values.every(uuid) && new Set(values).size === values.length;

function validCurrent(ledgerId: string, creditId: string, review: CreditRevisionReview): boolean {
    const { current: c, confirmation: original, asset } = review;
    if (
        !record(c) ||
        !record(original) ||
        !record(asset) ||
        !uuid(ledgerId) ||
        !uuid(creditId) ||
        c.credit_id !== creditId ||
        c.ledger_id !== ledgerId ||
        !keys(c, [
            "credit_id",
            "ledger_id",
            "document_id",
            "document_kind",
            "party_id",
            "asset_id",
            "version",
            "status",
            "revision_id",
            "revision_reason",
            "credit_date",
            "credit_reason",
            "members",
        ]) ||
        !uuid(c.document_id) ||
        !uuid(c.party_id) ||
        !["invoice", "bill"].includes(c.document_kind) ||
        c.status !== "active" ||
        !version(c.version, 2147483646) ||
        !date(c.credit_date) ||
        !reason(c.credit_reason, 2000) ||
        (c.version === 1
            ? c.revision_id !== null || c.revision_reason !== null
            : !uuid(c.revision_id) ||
              c.revision_id === creditId ||
              !reason(c.revision_reason, 1000)) ||
        typeof asset.asset_id !== "string" ||
        !asset.asset_id ||
        asset.asset_id !== c.asset_id ||
        typeof asset.code !== "string" ||
        !asset.code ||
        !["fiat", "native", "token"].includes(asset.kind) ||
        !Number.isInteger(asset.scale) ||
        asset.scale < 0 ||
        asset.scale > 18 ||
        typeof asset.enabled !== "boolean" ||
        (asset.kind === "token"
            ? typeof asset.network !== "string" ||
              !asset.network ||
              typeof asset.token_reference !== "string" ||
              !asset.token_reference ||
              asset.asset_id !== `token:${asset.network}:${asset.token_reference}`
            : asset.asset_id !== asset.code ||
              asset.token_reference !== null ||
              (asset.kind === "fiat"
                  ? asset.network !== null
                  : typeof asset.network !== "string" || !asset.network)) ||
        original.kind !== "business_confirmation" ||
        !uuid(original.id) ||
        original.ledger_id !== ledgerId ||
        original.document_id !== c.document_id ||
        !version(original.source_version, 2147483646) ||
        original.document_version !== original.source_version + 1 ||
        !Array.isArray(original.postings) ||
        !original.postings.length ||
        original.postings.length > 200 ||
        !original.postings.every(record) ||
        !Array.isArray(c.members) ||
        !c.members.length ||
        c.members.length > 200 ||
        !c.members.every(record)
    )
        return false;
    for (const key of ["line_id", "operation_id", "journal_id"] as const)
        if (!distinctIds(original.postings.map((row) => row[key]))) return false;
    if (
        !original.postings.every(
            (row, i) =>
                version(row.line_no) && (!i || row.line_no > original.postings[i - 1].line_no),
        )
    )
        return false;
    for (const key of [
        "source_line_id",
        "source_operation_id",
        "operation_id",
        "journal_id",
    ] as const)
        if (!distinctIds(c.members.map((row) => row[key]))) return false;
    const sourceOperations = new Set(original.postings.map((row) => row.operation_id));
    const sourceJournals = new Set(original.postings.map((row) => row.journal_id));
    return c.members.every((row, index) => {
        const source = original.postings.find((item) => item.line_id === row.source_line_id);
        const net = quantity(row.net_amount, asset.scale),
            tax = quantity(row.tax_amount, asset.scale);
        return (
            keys(row, [
                "line_no",
                "source_line_id",
                "source_operation_id",
                "operation_id",
                "operation_version",
                "journal_id",
                "net_amount",
                "tax_amount",
                "total_amount",
                "recognition_date",
            ]) &&
            row.line_no === index + 1 &&
            version(row.operation_version, c.version) &&
            row.source_operation_id === source?.operation_id &&
            !sourceOperations.has(row.operation_id) &&
            !sourceJournals.has(row.journal_id) &&
            date(row.recognition_date) &&
            ![row.net_amount, row.tax_amount, row.total_amount].some((value) =>
                value.startsWith("-"),
            ) &&
            net + tax > 0n &&
            net + tax === quantity(row.total_amount, asset.scale)
        );
    });
}

/** Independent GET snapshots are a preflight only; the write service decides live capacity. */
function validReplacement(
    ledgerId: string,
    creditId: string,
    body: CreditCorrection,
    review: CreditRevisionReview,
): boolean {
    const { capacity, current, confirmation, asset } = review;
    if (
        !record(body.replacement) ||
        !keys(body.replacement, ["credit_date", "reason", "lines"]) ||
        !reason(body.replacement.reason, 2000) ||
        !capacity ||
        !record(capacity) ||
        !Array.isArray(capacity.lines) ||
        !capacity.lines.every(record) ||
        capacity.document_kind !== current.document_kind ||
        capacity.party_id !== current.party_id ||
        !distinctIds(capacity.lines.map((row) => row.source_line_id)) ||
        capacity.lines.length !== confirmation.postings.length ||
        !capacity.lines.every((row) =>
            confirmation.postings.some((item) => item.line_id === row.source_line_id),
        )
    )
        return false;
    const input = {
        id: creditId,
        document_id: current.document_id,
        expected_version: confirmation.document_version,
        ...body.replacement,
        // Revision text was validated above with server Unicode semantics. Keep
        // legacy creation rules unchanged while reusing their amount/capacity checks.
        reason: "Validated revision fact reason",
    };
    if (
        !validCreditInput(input) ||
        input.lines.some(
            (line) => line.net_amount.startsWith("-") || line.tax_amount.startsWith("-"),
        )
    )
        return false;
    const adjusted = capacity.lines.map((row) => {
        const own = current.members.find((item) => item.source_line_id === row.source_line_id);
        const net = quantity(row.net_amount, asset.scale),
            tax = quantity(row.tax_amount, asset.scale);
        const creditedNet = quantity(row.credited_net_amount, asset.scale),
            creditedTax = quantity(row.credited_tax_amount, asset.scale);
        const ownNet = own ? quantity(own.net_amount, asset.scale) : 0n;
        const ownTax = own ? quantity(own.tax_amount, asset.scale) : 0n;
        const applied = quantity(row.applied_amount, asset.scale);
        if (
            net + tax !== quantity(row.total_amount, asset.scale) ||
            ownNet > creditedNet ||
            ownTax > creditedTax ||
            net - creditedNet !== quantity(row.remaining_net_amount, asset.scale) ||
            tax - creditedTax !== quantity(row.remaining_tax_amount, asset.scale) ||
            net + tax - applied - creditedNet - creditedTax !==
                quantity(row.available_gross_amount, asset.scale)
        )
            throw new Error("Inconsistent capacity snapshot");
        const amount = (value: bigint) => amountFromMinorUnits(value, asset.scale);
        return {
            ...row,
            credited_net_amount: amount(creditedNet - ownNet),
            credited_tax_amount: amount(creditedTax - ownTax),
            remaining_net_amount: amount(net - creditedNet + ownNet),
            remaining_tax_amount: amount(tax - creditedTax + ownTax),
            available_gross_amount: amount(
                net + tax - applied - creditedNet - creditedTax + ownNet + ownTax,
            ),
        };
    });
    return validCreditReview(ledgerId, input, {
        confirmation,
        asset,
        capacity: { ...capacity, lines: adjusted },
    });
}

export function validCreditRevisionReview(
    ledgerId: string,
    creditId: string,
    action: CreditRevisionAction,
    body: unknown,
    value: unknown,
): body is CreditCorrection | CreditCancellation {
    try {
        if (
            !record(body) ||
            !record(value) ||
            !["correct", "cancel"].includes(action) ||
            !keys(
                body,
                action === "correct"
                    ? ["expected_version", "reason", "replacement"]
                    : ["expected_version", "reason"],
            ) ||
            !version(body.expected_version, 2147483646) ||
            !reason(body.reason, 1000, true)
        )
            return false;
        const review = value as unknown as CreditRevisionReview;
        if (
            !validCurrent(ledgerId, creditId, review) ||
            body.expected_version !== review.current.version
        )
            return false;
        // Cancellation deliberately does not require enabled references or a capacity read.
        return (
            action === "cancel" ||
            validReplacement(ledgerId, creditId, body as CreditCorrection, review)
        );
    } catch {
        return false;
    }
}
