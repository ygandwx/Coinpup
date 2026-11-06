import { ApiError } from "./api";
import { getBusinessConfirmation } from "./business-confirmation-api";
import { getCreditCapacity, getCreditCurrent } from "./credit-api";
import type { CreditCurrent } from "./credit-api";
import type { Asset } from "./ledger-api";
import { date, record, uuid } from "./credit-intent";
import type { CreditRevisionAction, CreditRevisionReview } from "./credit-revision-intent";
import { stripCreditRevisionReason } from "./credit-revision-intent";
import type { CreditRevisionDraft } from "./credit-revision-fields";
import {
    canSubmitCreditRevision,
    initialCreditRevision,
    setCreditRevisionReason,
} from "./credit-revision-fields";

const invalid = () => new ApiError("server", 200, "invalid_response");

/** Check scope and fields used by the summary. Opening a form validates the full review. */
export function checkedCreditCurrent(
    value: CreditCurrent,
    ledgerId: string,
    creditId: string,
): CreditCurrent {
    const reason = (text: unknown, max: number) =>
        typeof text === "string" &&
        !!stripCreditRevisionReason(text) &&
        !text.includes("\0") &&
        [...text].length <= max;
    if (
        !record(value) ||
        value.credit_id !== creditId ||
        value.ledger_id !== ledgerId ||
        !uuid(value.document_id) ||
        !uuid(value.party_id) ||
        !["invoice", "bill"].includes(value.document_kind) ||
        typeof value.asset_id !== "string" ||
        !value.asset_id ||
        value.asset_id.length > 200 ||
        value.asset_id.includes("\0") ||
        !Number.isSafeInteger(value.version) ||
        value.version < 1 ||
        value.version > 2147483647 ||
        !["active", "cancelled"].includes(value.status) ||
        !Array.isArray(value.members) ||
        (value.version === 1
            ? value.revision_id !== null || value.revision_reason !== null
            : !uuid(value.revision_id) || !reason(value.revision_reason, 1000)) ||
        (value.status === "active"
            ? !date(value.credit_date) ||
              !reason(value.credit_reason, 2000) ||
              !value.members.length ||
              value.members.length > 200
            : value.version < 2 ||
              value.credit_date !== null ||
              value.credit_reason !== null ||
              value.members.length !== 0)
    )
        throw invalid();
    return value;
}

export type CreditRevisionEditor = { draft: CreditRevisionDraft; review: CreditRevisionReview };

/** Only opens a new editor or explicitly refreshes a rejected draft; never resolves a POST. */
export async function loadCreditRevisionEditor(
    ledgerId: string,
    creditId: string,
    action: CreditRevisionAction,
    assets: Asset[],
    signal: AbortSignal,
    expectedVersion?: number,
): Promise<CreditRevisionEditor> {
    const current = checkedCreditCurrent(
        await getCreditCurrent(ledgerId, creditId, signal),
        ledgerId,
        creditId,
    );
    if (current.status === "cancelled") throw new ApiError("server", 409, "credit_cancelled");
    if (current.version >= 2147483647) throw new ApiError("server", 409, "credit_revision_version");
    if (expectedVersion !== undefined && current.version !== expectedVersion)
        throw new ApiError("server", 409, "version_conflict");
    const asset = assets.find((row) => row.asset_id === current.asset_id);
    if (!asset) throw invalid();
    if (action === "correct" && !asset.enabled) throw new ApiError("server", 409, "asset_disabled");
    const [source, capacity] = await Promise.all([
        getBusinessConfirmation(ledgerId, current.document_id, signal),
        action === "correct"
            ? getCreditCapacity(ledgerId, current.document_id, 0, signal)
            : undefined,
    ]);
    if (
        source.document.id !== current.document_id ||
        source.document.ledger_id !== ledgerId ||
        source.document.asset_id !== current.asset_id ||
        source.document.party_id !== current.party_id ||
        source.document.document_kind !== current.document_kind ||
        source.document.state !== "confirmed" ||
        source.document.version !== source.confirmation.document_version
    )
        throw invalid();
    const review: CreditRevisionReview = {
        current,
        confirmation: source.confirmation,
        asset: { ...asset },
        ...(capacity ? { capacity } : {}),
    };
    const draft = initialCreditRevision(action, review);
    // Validate the untouched source using an internal non-empty reason, then discard it.
    // The user's actual new reason remains blank; no intent/key is created by these reads.
    if (
        !canSubmitCreditRevision(
            ledgerId,
            creditId,
            setCreditRevisionReason(draft, "Source review validation"),
            review,
        )
    )
        throw invalid();
    return { draft, review };
}
