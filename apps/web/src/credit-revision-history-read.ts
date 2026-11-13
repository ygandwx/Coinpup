import { ApiError } from "./api";
import { getCredit, getCreditRevision, listCreditHistory } from "./credit-api";
import type { CreditHistoryEntry } from "./credit-api";
import { uuid } from "./credit-intent";
import { creditHistoryDisplay } from "./credit-revision-history-display";
import type { CreditHistoryScope, CreditHistoryValue } from "./credit-revision-history-display";

export type CreditHistoryRead = Readonly<
    CreditHistoryScope & {
        key: string;
        offset: number;
        selected: CreditHistoryEntry | null;
    }
>;

/** One bounded GET, never a command recovery or a batch of member/detail reads. */
export async function readCreditHistory(
    request: CreditHistoryRead,
    signal: AbortSignal,
    isCurrent: () => boolean,
    publish: {
        loaded: (value: CreditHistoryValue) => void;
        failed: (error: unknown) => void;
        unauthorized: () => void;
    },
): Promise<void> {
    const active = () => !signal.aborted && isCurrent();
    if (!active()) return;
    try {
        const { ledgerId, creditId, documentId, assetId, offset, selected } = request;
        if (
            !uuid(ledgerId) ||
            !uuid(creditId) ||
            !uuid(documentId) ||
            !assetId ||
            !Number.isSafeInteger(offset) ||
            offset < 0 ||
            offset > 100000 ||
            offset % 25 !== 0 ||
            (selected !== null &&
                (!Number.isSafeInteger(selected.version) ||
                    selected.version < 1 ||
                    selected.version > 2147483647 ||
                    (selected.version === 1
                        ? selected.action !== "create" || selected.revision_id !== null
                        : !["correct", "cancel"].includes(selected.action) ||
                          !uuid(selected.revision_id))))
        )
            throw new ApiError("server", 200, "invalid_response");
        const value =
            selected === null
                ? await listCreditHistory(ledgerId, creditId, offset, signal)
                : selected.action === "create"
                  ? await getCredit(ledgerId, creditId, signal)
                  : await getCreditRevision(ledgerId, creditId, selected.revision_id!, signal);
        if (!active()) return;
        if (!creditHistoryDisplay(value, request, offset, selected))
            throw new ApiError("server", 200, "invalid_response");
        publish.loaded(value);
    } catch (error) {
        if (!active()) return;
        publish.failed(error);
        if (error instanceof ApiError && error.status === 401 && active()) publish.unauthorized();
    }
}
