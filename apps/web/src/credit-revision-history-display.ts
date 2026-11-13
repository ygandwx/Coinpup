import type { CreditDetail, CreditHistoryEntry, CreditRevisionReceipt } from "./credit-api";
import { date, quantity, record, uuid } from "./credit-intent";
export type CreditHistoryValue = CreditHistoryEntry[] | CreditDetail | CreditRevisionReceipt;
export type CreditHistoryScope = {
    ledgerId: string;
    creditId: string;
    documentId: string;
    assetId: string;
};
type Fact = { net: string; tax: string; total: string; date: string };
type Line = {
    action: "create" | "correct" | "cancel";
    source: string;
    sourceOperation: string;
    operation: string;
    version: number;
    previousVersion: number | null;
    before: Fact | null;
    after: Fact | null;
    previousJournal: string | null;
    reversalJournal: string | null;
    journal: string | null;
};
export type CreditHistoryDisplay =
    | { kind: "page"; entries: CreditHistoryEntry[] }
    | {
          kind: "detail";
          action: CreditHistoryEntry["action"];
          version: number;
          previousVersion: number | null;
          reason: string;
          receiptId: string;
          previousDate: string | null;
          previousReason: string | null;
          creditDate: string | null;
          creditReason: string | null;
          lines: Line[];
      };
const integer = (v: unknown, max = 2147483647): v is number =>
    Number.isSafeInteger(v) && (v as number) >= 1 && (v as number) <= max;
const text = (v: unknown): v is string =>
    typeof v === "string" && v.length > 0 && !v.includes("\0") && [...v].length <= 2000;
function entry(v: unknown): v is CreditHistoryEntry {
    return (
        record(v) &&
        integer(v.version) &&
        text(v.reason) &&
        typeof v.created_at === "string" &&
        Number.isFinite(Date.parse(v.created_at)) &&
        (v.version === 1
            ? v.action === "create" && v.revision_id === null
            : ["correct", "cancel"].includes(v.action as string) && uuid(v.revision_id))
    );
}
function fact(v: Record<string, unknown>, prefix = ""): Fact {
    const net = v[prefix + "net_amount"],
        tax = v[prefix + "tax_amount"],
        total = v[prefix + "total_amount"],
        when = v[prefix + "recognition_date"];
    if (
        [net, tax, total].some((n) => typeof n !== "string" || n.startsWith("-")) ||
        !date(when) ||
        quantity(net, 18) + quantity(tax, 18) !== quantity(total, 18) ||
        quantity(total, 18) === 0n
    )
        throw new Error("Invalid display quantities");
    return { net: net as string, tax: tax as string, total: total as string, date: when };
}
const absent = (v: Record<string, unknown>, names: string[]) =>
    names.every((name) => v[name] === null);
/** Display guards only. A history GET never proves the outcome of a pending command. */
export function creditHistoryDisplay(
    value: unknown,
    scope: CreditHistoryScope,
    offset: number,
    selected: CreditHistoryEntry | null,
): CreditHistoryDisplay | null {
    try {
        if (
            !uuid(scope.ledgerId) ||
            !uuid(scope.creditId) ||
            !uuid(scope.documentId) ||
            !scope.assetId ||
            !Number.isInteger(offset) ||
            offset < 0 ||
            offset > 100000
        )
            return null;
        if (selected === null) {
            if (
                !Array.isArray(value) ||
                value.length > 25 ||
                !value.every(entry) ||
                value.some((row, i) => row.version !== offset + i + 1) ||
                new Set(value.map((row) => row.revision_id)).size !== value.length
            )
                return null;
            return { kind: "page", entries: value };
        }
        if (
            !entry(selected) ||
            !record(value) ||
            value.ledger_id !== scope.ledgerId ||
            value.document_id !== scope.documentId ||
            value.asset_id !== scope.assetId
        )
            return null;
        if (selected.action === "create") {
            const r = value.receipt;
            if (
                value.id !== scope.creditId ||
                !date(value.credit_date) ||
                value.reason !== selected.reason ||
                !integer(value.source_version) ||
                !record(r) ||
                r.kind !== "business_credit" ||
                r.id !== scope.creditId ||
                r.ledger_id !== scope.ledgerId ||
                r.document_id !== scope.documentId ||
                r.asset_id !== scope.assetId ||
                r.credit_date !== value.credit_date ||
                r.source_version !== value.source_version ||
                !Array.isArray(r.postings) ||
                r.postings.length < 1 ||
                r.postings.length > 200
            )
                return null;
            const sources = new Set(),
                operations = new Set(),
                journals = new Set();
            const lines: Line[] = r.postings.map((row, i) => {
                if (
                    !record(row) ||
                    row.line_no !== i + 1 ||
                    !uuid(row.source_line_id) ||
                    sources.has(row.source_line_id) ||
                    !uuid(row.source_operation_id) ||
                    !uuid(row.operation_id) ||
                    operations.has(row.operation_id) ||
                    !uuid(row.journal_id) ||
                    journals.has(row.journal_id)
                )
                    throw new Error("Invalid posting");
                sources.add(row.source_line_id);
                operations.add(row.operation_id);
                journals.add(row.journal_id);
                return {
                    action: "create",
                    source: row.source_line_id,
                    sourceOperation: row.source_operation_id,
                    operation: row.operation_id,
                    version: 1,
                    previousVersion: null,
                    before: null,
                    after: fact(row),
                    previousJournal: null,
                    reversalJournal: null,
                    journal: row.journal_id,
                };
            });
            return {
                kind: "detail",
                action: "create",
                version: 1,
                previousVersion: null,
                reason: selected.reason,
                receiptId: scope.creditId,
                previousDate: null,
                previousReason: null,
                creditDate: value.credit_date,
                creditReason: selected.reason,
                lines,
            };
        }
        if (
            value.kind !== "business_credit_revision" ||
            value.credit_id !== scope.creditId ||
            value.revision_id !== selected.revision_id ||
            value.version !== selected.version ||
            value.previous_version !== selected.version - 1 ||
            value.action !== selected.action ||
            value.reason !== selected.reason ||
            !date(value.previous_credit_date) ||
            !text(value.previous_credit_reason) ||
            !Array.isArray(value.members) ||
            value.members.length < 1 ||
            value.members.length > 400 ||
            (selected.action === "correct"
                ? !date(value.credit_date) || !text(value.replacement_reason)
                : value.credit_date !== null || value.replacement_reason !== null)
        )
            return null;
        let next = 0,
            previous = 0,
            lastRemoved = 0;
        const sources = new Set(),
            operations = new Set(),
            journals = new Set(),
            positions = new Set();
        const lines: Line[] = value.members.map((row) => {
            if (
                !record(row) ||
                !["create", "correct", "cancel"].includes(row.action as string) ||
                !uuid(row.source_line_id) ||
                sources.has(row.source_line_id) ||
                !uuid(row.source_operation_id) ||
                !uuid(row.operation_id) ||
                operations.has(row.operation_id) ||
                !integer(row.operation_version)
            )
                throw new Error("Invalid transition");
            const old = row.action !== "create",
                added = row.action !== "cancel";
            if (
                old
                    ? !integer(row.previous_line_no, 200) ||
                      positions.has(row.previous_line_no) ||
                      !integer(row.previous_operation_version) ||
                      row.operation_version !== row.previous_operation_version + 1 ||
                      !uuid(row.previous_journal_id) ||
                      !uuid(row.reversal_journal_id)
                    : row.operation_version !== 1 ||
                      !absent(row, [
                          "previous_line_no",
                          "previous_operation_version",
                          "previous_journal_id",
                          "reversal_journal_id",
                          "previous_net_amount",
                          "previous_tax_amount",
                          "previous_total_amount",
                          "previous_recognition_date",
                      ])
            )
                throw new Error("Invalid previous fact");
            if (
                added
                    ? selected.action === "cancel" ||
                      lastRemoved > 0 ||
                      row.line_no !== ++next ||
                      !uuid(row.journal_id)
                    : !absent(row, [
                          "line_no",
                          "journal_id",
                          "net_amount",
                          "tax_amount",
                          "total_amount",
                          "recognition_date",
                      ]) || (row.previous_line_no as number) <= lastRemoved
            )
                throw new Error("Invalid replacement");
            if (old) {
                previous += 1;
                positions.add(row.previous_line_no);
            }
            if (!added) lastRemoved = row.previous_line_no as number;
            sources.add(row.source_line_id);
            operations.add(row.operation_id);
            for (const journal of [
                row.previous_journal_id,
                row.reversal_journal_id,
                row.journal_id,
            ]) {
                if (journal === null) continue;
                if (!uuid(journal) || journals.has(journal))
                    throw new Error("Invalid journal identity");
                journals.add(journal);
            }
            return {
                action: row.action as Line["action"],
                source: row.source_line_id,
                sourceOperation: row.source_operation_id,
                operation: row.operation_id,
                version: row.operation_version,
                previousVersion: row.previous_operation_version as number | null,
                before: old ? fact(row, "previous_") : null,
                after: added ? fact(row) : null,
                previousJournal: row.previous_journal_id as string | null,
                reversalJournal: row.reversal_journal_id as string | null,
                journal: row.journal_id as string | null,
            };
        });
        if (
            previous < 1 ||
            previous > 200 ||
            next > 200 ||
            (selected.action === "correct" && next === 0) ||
            [...positions].some((position) => !integer(position, previous))
        )
            return null;
        return {
            kind: "detail",
            action: selected.action,
            version: selected.version,
            previousVersion: selected.version - 1,
            reason: selected.reason,
            receiptId: selected.revision_id!,
            previousDate: value.previous_credit_date,
            previousReason: value.previous_credit_reason,
            creditDate: value.credit_date as string | null,
            creditReason: value.replacement_reason as string | null,
            lines,
        };
    } catch {
        return null;
    }
}
