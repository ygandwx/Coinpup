import type { ReturnRevisionReceipt } from "./allocation-return-revision-api";
import {
    reviewedSources,
    returnScope,
    sameReturnAmounts,
    sameSource,
    validReturnRevisionReview,
} from "./allocation-return-revision-intent";
import type { ReturnRevisionInput } from "./allocation-return-revision-intent";
import { validReturnRevision } from "./allocation-return-revision-facts";
import { record, uuid } from "./refund-intent";
import { stripCommandText } from "./command-text";

export type FrozenReturnRevision = Readonly<{
    ownerId: string;
    ledgerId: string;
    returnId: string;
    action: "correct" | "cancel";
    key: string;
    bodyJson: string;
    reviewedJson: string;
}>;

export function validFrozenReturnRevision(
    value: unknown,
    plan: FrozenReturnRevision,
): value is ReturnRevisionReceipt {
    try {
        const input: ReturnRevisionInput = {
            action: plan.action,
            returnId: plan.returnId,
            body: JSON.parse(plan.bodyJson),
        };
        const review: unknown = JSON.parse(plan.reviewedJson);
        if (
            !validReturnRevisionReview(plan.ledgerId, input, review) ||
            !record(value) ||
            !uuid(value.revision_id)
        )
            return false;
        const { current, original, assets } = review;
        const selected = {
            version: current.version + 1,
            action: input.action,
            reason: stripCommandText(input.body.reason),
            revision_id: value.revision_id,
        };
        if (
            !validReturnRevision(value, returnScope(original), selected, assets) ||
            value.revision_id === current.revision_id ||
            value.previous.return_date !== current.fact!.return_date ||
            value.previous.reason !== current.fact!.reason
        )
            return false;
        const replace = input.action === "correct" ? input.body.replacement : null;
        if (
            replace &&
            (!value.replacement ||
                value.replacement.return_date !== replace.return_date ||
                value.replacement.reason !== stripCommandText(replace.reason))
        )
            return false;
        const lines = replace?.lines ?? [];
        const old = new Map(current.members.map((m) => [m.source_allocation_line_id, m]));
        const newIds = new Set(lines.map((l) => l.source_allocation_line_id));
        const removed = current.members.filter((m) => !newIds.has(m.source_allocation_line_id));
        if (value.members.length !== lines.length + removed.length) return false;
        const sources = reviewedSources(replace ? review.capacities : []);
        const originalMembers = new Map(
            original.lines.map((m) => [m.source_allocation_line_id, m]),
        );
        const originalOperations = new Set(original.lines.map((m) => m.operation_id));
        const originalJournals = new Set(original.lines.map((m) => m.journal_id));
        const scope = current.payment_asset_id;
        return value.members.every((m, i) => {
            const id = m.source_allocation_line_id,
                previous = old.get(id),
                line = lines[i];
            if (
                id !==
                (line?.source_allocation_line_id ??
                    removed[i - lines.length]?.source_allocation_line_id)
            )
                return false;
            if (previous) {
                if (
                    m.action !== (line ? "correct" : "cancel") ||
                    !m.previous ||
                    !sameSource(previous, m) ||
                    m.document_asset_id !== previous.document_asset_id ||
                    m.source_return_no !== previous.source_return_no ||
                    m.operation_id !== previous.operation_id ||
                    m.operation_version !== previous.operation_version + 1 ||
                    m.previous.line_no !== previous.line_no ||
                    m.previous.journal_id !== previous.journal_id ||
                    !sameReturnAmounts(m.previous, previous, scope, m.document_asset_id, assets)
                )
                    return false;
            } else {
                const originalMember = originalMembers.get(id);
                if (
                    m.action !== "create" ||
                    originalOperations.has(m.operation_id) ||
                    // An original member already occupied a permanent slot on this source.
                    (originalMember &&
                        (m.source_return_no === "1" ||
                            !sameSource(originalMember, m) ||
                            originalMember.document_asset_id !== m.document_asset_id))
                )
                    return false;
            }
            if (
                (m.reversal_journal_id && originalJournals.has(m.reversal_journal_id)) ||
                (m.replacement && originalJournals.has(m.replacement.journal_id))
            )
                return false;
            if (!line) return m.replacement === null;
            const source = sources.get(id)?.source;
            return (
                !!source &&
                sameSource(source, m) &&
                source.document_asset_id === m.document_asset_id &&
                !!m.replacement &&
                sameReturnAmounts(m.replacement, line, scope, m.document_asset_id, assets)
            );
        });
    } catch {
        return false;
    }
}
