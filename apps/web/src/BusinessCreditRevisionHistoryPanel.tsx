import { useEffect, useMemo, useRef, useState } from "react";
import type { Session } from "./api";
import type { CreditHistoryEntry } from "./credit-api";
import type { Locale } from "./i18n";
import { BusinessCreditRevisionHistory } from "./BusinessCreditRevisionHistory";
import { readCreditHistory } from "./credit-revision-history-read";
import type { CreditHistoryRead } from "./credit-revision-history-read";
import type { CreditHistoryScope, CreditHistoryValue } from "./credit-revision-history-display";

/** Mounted only outside command editors/results. No financial controller is accepted. */
export function BusinessCreditRevisionHistoryPanel({
    ledgerId,
    creditId,
    documentId,
    assetId,
    session,
    locale,
    available,
    onUnauthorized,
}: CreditHistoryScope & {
    session: Session;
    locale: Locale;
    available: boolean;
    onUnauthorized: () => void;
}) {
    const [offset, setOffset] = useState(0);
    const [selected, setSelected] = useState<CreditHistoryEntry | null>(null);
    const [refresh, setRefresh] = useState(0);
    const request = useMemo(
        () => ({
            // Session identity participates in the request, without putting credentials in a key.
            session,
            available,
            read: {
                ledgerId,
                creditId,
                documentId,
                assetId,
                offset,
                selected,
                key: JSON.stringify([
                    session.user.id,
                    ledgerId,
                    creditId,
                    documentId,
                    assetId,
                    offset,
                    selected?.action,
                    selected?.version,
                    selected?.revision_id,
                    refresh,
                ]),
            } satisfies CreditHistoryRead,
        }),
        [session, available, ledgerId, creditId, documentId, assetId, offset, selected, refresh],
    );
    const [loaded, setLoaded] = useState<{
        request: typeof request;
        value: CreditHistoryValue;
    } | null>(null);
    const [failure, setFailure] = useState<{
        request: typeof request;
        error: unknown;
    } | null>(null);
    const latest = useRef({ request, available, onUnauthorized });
    latest.current = { request, available, onUnauthorized };
    useEffect(() => {
        if (!available) return;
        const abort = new AbortController();
        void readCreditHistory(
            request.read,
            abort.signal,
            () => latest.current.available && latest.current.request === request,
            {
                loaded: (value) => {
                    setLoaded({ request, value });
                    setFailure(null);
                },
                failed: (error) => {
                    setFailure({ request, error });
                    setLoaded(null);
                },
                unauthorized: () => latest.current.onUnauthorized(),
            },
        );
        return () => abort.abort();
    }, [request, available]);
    return (
        <BusinessCreditRevisionHistory
            ledgerId={ledgerId}
            creditId={creditId}
            documentId={documentId}
            assetId={assetId}
            locale={locale}
            available={available}
            requestKey={request.read.key}
            offset={offset}
            selected={selected}
            loaded={
                loaded?.request === request ? { key: request.read.key, value: loaded.value } : null
            }
            failure={
                failure?.request === request
                    ? { key: request.read.key, error: failure.error }
                    : null
            }
            onSelect={(entry) => setSelected(entry ? { ...entry } : null)}
            onPage={(next) => {
                if (!Number.isSafeInteger(next) || next < 0 || next > 100000 || next % 25 !== 0)
                    return;
                setSelected(null);
                setOffset(next);
            }}
            onReload={() => setRefresh((value) => value + 1)}
        />
    );
}
