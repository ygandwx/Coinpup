import type { CreditCorrection, CreditRevisionReceipt } from "./credit-api";
import { keys, quantity, record, uuid } from "./credit-intent";
import { stripCreditRevisionReason, validCreditRevisionReview } from "./credit-revision-intent";
import type { CreditRevisionReview, FrozenCreditRevision } from "./credit-revision-intent";

/** Only the complete response to the frozen original command proves its outcome. */
export function validCreditRevisionReceipt(
    value: unknown,
    plan: FrozenCreditRevision,
): value is CreditRevisionReceipt {
    try {
        const body = JSON.parse(plan.bodyJson) as CreditCorrection;
        const review = JSON.parse(plan.reviewedJson) as CreditRevisionReview;
        const c = review.current;
        if (
            !validCreditRevisionReview(plan.ledgerId, plan.creditId, plan.action, body, review) ||
            !record(value) ||
            !keys(value, [
                "kind",
                "credit_id",
                "revision_id",
                "ledger_id",
                "document_id",
                "asset_id",
                "previous_version",
                "version",
                "action",
                "reason",
                "previous_credit_date",
                "previous_credit_reason",
                "credit_date",
                "replacement_reason",
                "members",
            ]) ||
            value.kind !== "business_credit_revision" ||
            value.credit_id !== plan.creditId ||
            !uuid(value.revision_id) ||
            value.revision_id === plan.creditId ||
            value.revision_id === c.revision_id ||
            value.ledger_id !== plan.ledgerId ||
            value.document_id !== c.document_id ||
            value.asset_id !== c.asset_id ||
            value.previous_version !== c.version ||
            value.version !== c.version + 1 ||
            value.action !== plan.action ||
            value.reason !== stripCreditRevisionReason(body.reason) ||
            value.previous_credit_date !== c.credit_date ||
            value.previous_credit_reason !== c.credit_reason ||
            value.credit_date !==
                (plan.action === "correct" ? body.replacement.credit_date : null) ||
            value.replacement_reason !==
                (plan.action === "correct"
                    ? stripCreditRevisionReason(body.replacement.reason)
                    : null) ||
            !Array.isArray(value.members)
        )
            return false;
        const lines = plan.action === "correct" ? body.replacement.lines : [];
        const transitions = [
            ...lines.map((line, index) => ({
                old: c.members.find((row) => row.source_line_id === line.source_line_id),
                line,
                position: index + 1,
            })),
            ...c.members
                .filter((row) => !lines.some((line) => line.source_line_id === row.source_line_id))
                .map((old) => ({ old, line: undefined, position: null })),
        ];
        if (value.members.length !== transitions.length) return false;
        const operations = new Set<string>(),
            journals = new Set<string>();
        const oldOperations = new Set(c.members.map((row) => row.operation_id));
        const sourceOperations = new Set(
            review.confirmation.postings.map((row) => row.operation_id),
        );
        const sourceJournals = new Set(review.confirmation.postings.map((row) => row.journal_id));
        return value.members.every((row, index) => {
            const { old, line, position } = transitions[index];
            const sourceId = line?.source_line_id ?? old!.source_line_id;
            const original = review.confirmation.postings.find(
                (item) => item.line_id === sourceId,
            )!;
            if (
                !record(row) ||
                !keys(row, [
                    "action",
                    "source_line_id",
                    "source_operation_id",
                    "operation_id",
                    "operation_version",
                    "previous_line_no",
                    "previous_operation_version",
                    "previous_journal_id",
                    "reversal_journal_id",
                    "previous_net_amount",
                    "previous_tax_amount",
                    "previous_total_amount",
                    "previous_recognition_date",
                    "line_no",
                    "journal_id",
                    "net_amount",
                    "tax_amount",
                    "total_amount",
                    "recognition_date",
                ]) ||
                row.action !== (!old ? "create" : line ? "correct" : "cancel") ||
                row.source_line_id !== sourceId ||
                row.source_operation_id !== original.operation_id ||
                !uuid(row.operation_id) ||
                operations.has(row.operation_id) ||
                sourceOperations.has(row.operation_id) ||
                (old
                    ? row.operation_id !== old.operation_id
                    : oldOperations.has(row.operation_id)) ||
                row.operation_version !== (old ? old.operation_version + 1 : 1) ||
                row.previous_line_no !== (old?.line_no ?? null) ||
                row.previous_operation_version !== (old?.operation_version ?? null) ||
                row.previous_journal_id !== (old?.journal_id ?? null) ||
                row.previous_recognition_date !== (old?.recognition_date ?? null) ||
                (old ? !uuid(row.reversal_journal_id) : row.reversal_journal_id !== null) ||
                row.line_no !== position ||
                (line ? !uuid(row.journal_id) : row.journal_id !== null) ||
                row.recognition_date !== (line?.recognition_date ?? null)
            )
                return false;
            operations.add(row.operation_id);
            for (const journal of [
                row.previous_journal_id,
                row.reversal_journal_id,
                row.journal_id,
            ]) {
                if (journal === null) continue;
                if (!uuid(journal) || journals.has(journal) || sourceJournals.has(journal))
                    return false;
                journals.add(journal);
            }
            for (const [prefix, expected] of [
                ["previous_", old],
                ["", line],
            ] as const) {
                const values = ["net_amount", "tax_amount", "total_amount"].map(
                    (key) => row[prefix + key],
                );
                if (!expected) {
                    if (values.some((v) => v !== null)) return false;
                } else {
                    if (values.some((v) => typeof v !== "string" || v.startsWith("-")))
                        return false;
                    const net = quantity(values[0], 18),
                        tax = quantity(values[1], 18);
                    if (
                        net !== quantity(expected.net_amount, 18) ||
                        tax !== quantity(expected.tax_amount, 18) ||
                        net + tax !== quantity(values[2], 18)
                    )
                        return false;
                }
            }
            return true;
        });
    } catch {
        return false;
    }
}
