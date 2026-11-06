import type { CreditCancellation, CreditCorrection } from "./credit-api";
import type { CreditRevisionAction, CreditRevisionReview } from "./credit-revision-intent";
import { validCreditRevisionReview } from "./credit-revision-intent";
import { quantity } from "./credit-intent";
import type { Locale } from "./i18n";
import { formatAmount, sumAmounts } from "./money";

export type CreditRevisionDraft =
    { action: "correct"; body: CreditCorrection } | { action: "cancel"; body: CreditCancellation };

export function initialCreditRevision(
    action: CreditRevisionAction,
    review: CreditRevisionReview,
): CreditRevisionDraft {
    const current = review.current;
    if (current.status !== "active" || !current.credit_date || current.credit_reason === null)
        throw new Error("An active credit is required");
    const body = { expected_version: current.version, reason: "" };
    return action === "cancel"
        ? { action, body }
        : {
              action,
              body: {
                  ...body,
                  replacement: {
                      credit_date: current.credit_date,
                      reason: current.credit_reason,
                      lines: current.members.map((row) => ({
                          source_line_id: row.source_line_id,
                          net_amount: row.net_amount,
                          tax_amount: row.tax_amount,
                          recognition_date: row.recognition_date,
                      })),
                  },
              },
          };
}

export function updateCreditCorrection(
    draft: CreditRevisionDraft,
    update: (body: CreditCorrection) => CreditCorrection,
): CreditRevisionDraft {
    return draft.action === "correct" ? { action: draft.action, body: update(draft.body) } : draft;
}

export function setCreditRevisionReason(
    draft: CreditRevisionDraft,
    reason: string,
): CreditRevisionDraft {
    return draft.action === "correct"
        ? { action: draft.action, body: { ...draft.body, reason } }
        : { action: draft.action, body: { ...draft.body, reason } };
}

export function selectCreditRevisionLine(
    draft: CreditRevisionDraft,
    review: CreditRevisionReview,
    sourceId: string,
    selected: boolean,
): CreditRevisionDraft {
    if (!review.capacity?.lines.some((row) => row.source_line_id === sourceId)) return draft;
    return updateCreditCorrection(draft, (body) => {
        const lines = body.replacement.lines;
        if (lines.some((row) => row.source_line_id === sourceId) === selected) return body;
        const previous = review.current.members.find((row) => row.source_line_id === sourceId);
        return {
            ...body,
            replacement: {
                ...body.replacement,
                lines: selected
                    ? [
                          ...lines,
                          {
                              source_line_id: sourceId,
                              net_amount: previous?.net_amount ?? "",
                              tax_amount: previous?.tax_amount ?? "",
                              recognition_date:
                                  previous?.recognition_date ?? body.replacement.credit_date,
                          },
                      ]
                    : lines.filter((row) => row.source_line_id !== sourceId),
            },
        };
    });
}

/** Display-only snapshot limits. The shared review and write service validate consistency. */
export function creditRevisionLimits(review: CreditRevisionReview, sourceId: string) {
    const source = review.capacity?.lines.find((row) => row.source_line_id === sourceId);
    if (!source) return null;
    const own = review.current.members.find((row) => row.source_line_id === sourceId);
    try {
        const add = (remaining: string, previous: string) => {
            if (remaining.startsWith("-") || previous.startsWith("-"))
                throw new Error("Invalid capacity quantity");
            quantity(remaining, review.asset.scale);
            quantity(previous, review.asset.scale);
            return sumAmounts([remaining, previous], review.asset.scale);
        };
        return {
            net: add(source.remaining_net_amount, own?.net_amount ?? "0"),
            tax: add(source.remaining_tax_amount, own?.tax_amount ?? "0"),
            gross: add(source.available_gross_amount, own?.total_amount ?? "0"),
        };
    } catch {
        return null;
    }
}

export function canSubmitCreditRevision(
    ledgerId: string,
    creditId: string,
    draft: CreditRevisionDraft,
    review: CreditRevisionReview,
): boolean {
    if (!validCreditRevisionReview(ledgerId, creditId, draft.action, draft.body, review))
        return false;
    return (
        draft.action === "cancel" ||
        (review.capacity?.lines.every(
            (row) => creditRevisionLimits(review, row.source_line_id) !== null,
        ) ??
            false)
    );
}

export function formatCreditRevisionAmount(
    value: unknown,
    asset: CreditRevisionReview["asset"],
    locale: Locale,
): string | null {
    try {
        if (typeof value !== "string" || value.startsWith("-")) return null;
        quantity(value, asset.scale);
        return `${formatAmount(value, locale)} ${asset.asset_id}`;
    } catch {
        return null;
    }
}
