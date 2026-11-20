import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import type { Asset } from "./ledger-api";
import type { Locale } from "./i18n";
import { getBusinessConfirmation } from "./business-confirmation-api";
import { listDocumentHistory, getDocumentRevision } from "./document-revision-api";
import type {
    DocumentCurrent,
    DocumentHistoryEntry,
    DocumentRevisionReceipt,
} from "./document-revision-api";
import {
    originalDocumentFacts,
    validDocumentHistoryPage,
    validDocumentHistoryReceipt,
    validDocumentHistoryEntry,
} from "./document-history-facts";
import type { DocumentHistoryScope } from "./document-history-facts";
import { BusinessDocumentCurrentFacts } from "./BusinessDocumentCurrentFacts";
import { BusinessDocumentRevisionFacts } from "./BusinessDocumentRevisionFacts";

type Display =
    | { kind: "page"; entries: DocumentHistoryEntry[] }
    | { kind: "original"; current: DocumentCurrent }
    | { kind: "revision"; receipt: DocumentRevisionReceipt };
/** Read-only history. No financial command controller or write callback is accepted. */
export function BusinessDocumentHistoryPanel({
    ledgerId,
    documentId,
    session,
    locale,
    assets,
    available,
    onUnauthorized,
    onBack,
}: {
    ledgerId: string;
    documentId: string;
    session: Session;
    locale: Locale;
    assets: Asset[];
    available: boolean;
    onUnauthorized: () => void;
    onBack: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [offset, setOffset] = useState(0),
        [refresh, setRefresh] = useState(0);
    const [selected, setSelected] = useState<DocumentHistoryEntry | null>(null);
    const scopeKey = JSON.stringify([session.user.id, ledgerId, documentId]);
    const request = useMemo(
        () => ({ session, scopeKey, offset, selected, refresh, available }),
        [session, scopeKey, offset, selected, refresh, available],
    );
    const [loaded, setLoaded] = useState<{ request: typeof request; display: Display } | null>(
        null,
    );
    const [failure, setFailure] = useState<{ request: typeof request; error: unknown } | null>(
        null,
    );
    const anchor = useRef<{
        session: Session;
        scopeKey: string;
        refresh: number;
        current: DocumentCurrent;
    } | null>(null);
    const latest = useRef({ request, assets, onUnauthorized });
    latest.current = { request, assets, onUnauthorized };
    useEffect(() => {
        if (!available) return;
        const abort = new AbortController(),
            active = () => !abort.signal.aborted && latest.current.request === request;
        async function read() {
            try {
                let original =
                    anchor.current?.session === session &&
                    anchor.current.scopeKey === scopeKey &&
                    anchor.current.refresh === refresh
                        ? anchor.current.current
                        : null;
                if (!original) {
                    const raw = await getBusinessConfirmation(ledgerId, documentId, abort.signal);
                    if (!active()) return;
                    original = originalDocumentFacts(
                        raw,
                        ledgerId,
                        documentId,
                        latest.current.assets,
                    );
                    if (!original) throw new ApiError("server", 200, "invalid_response");
                    anchor.current = { session, scopeKey, refresh, current: original };
                }
                const scope: DocumentHistoryScope = {
                    ledgerId,
                    documentId,
                    confirmationId: original.confirmation_id,
                    originalVersion: original.version,
                };
                let display: Display;
                if (!selected) {
                    const entries = await listDocumentHistory(
                        ledgerId,
                        documentId,
                        offset,
                        abort.signal,
                    );
                    if (!active()) return;
                    if (!validDocumentHistoryPage(entries, scope, offset))
                        throw new ApiError("server", 200, "invalid_response");
                    display = { kind: "page", entries };
                } else {
                    if (!validDocumentHistoryEntry(selected, scope))
                        throw new ApiError("server", 200, "invalid_response");
                    if (selected.action === "confirm")
                        display = { kind: "original", current: original };
                    else {
                        const receipt = await getDocumentRevision(
                            ledgerId,
                            documentId,
                            selected.revision_id!,
                            abort.signal,
                        );
                        if (!active()) return;
                        if (
                            !validDocumentHistoryReceipt(
                                receipt,
                                scope,
                                selected,
                                latest.current.assets,
                            )
                        )
                            throw new ApiError("server", 200, "invalid_response");
                        display = { kind: "revision", receipt };
                    }
                }
                if (active()) {
                    setLoaded({ request, display });
                    setFailure(null);
                }
            } catch (error) {
                if (!active()) return;
                setLoaded(null);
                setFailure({ request, error });
                if (error instanceof ApiError && error.status === 401) {
                    anchor.current = null;
                    latest.current.onUnauthorized();
                }
            }
        }
        void read();
        return () => abort.abort();
    }, [request, available, ledgerId, documentId, session, scopeKey, selected, offset, refresh]);
    const display = loaded?.request === request ? loaded.display : null;
    const error = failure?.request === request ? failure.error : null;
    if (!available || (error instanceof ApiError && error.status === 401))
        return <p role="alert">{t("请登录后重新读取历史。", "Sign in to reload history.")}</p>;
    const action = (v: DocumentHistoryEntry["action"]) =>
        v === "confirm"
            ? t("原始确认", "Original confirmation")
            : v === "correct"
              ? t("更正", "Correction")
              : t("取消", "Cancellation");
    const page = (next: number) => {
        if (Number.isSafeInteger(next) && next >= 0 && next <= 100000 && next % 25 === 0) {
            setSelected(null);
            setOffset(next);
        }
    };
    return (
        <section
            className="document-revision-form"
            aria-label={t("单据版本历史", "Document version history")}
        >
            <h3>{t("单据版本历史", "Document version history")}</h3>
            <p>
                {t(
                    "历史详情保留当时的完整事实，不代表当前状态，也不证明待确认请求已成功。",
                    "History preserves complete fixed facts. It is not current state and does not prove an unresolved request succeeded.",
                )}
            </p>
            {selected && (
                <h4>
                    {t("版本", "Version")} {selected.version} · {action(selected.action)}
                </h4>
            )}
            {!display && !error && <p role="status">{t("正在读取历史…", "Loading history…")}</p>}
            {error != null && (
                <p role="alert">
                    {t(
                        "无法核对完整历史，请重新读取。",
                        "The complete history could not be verified. Reload to try again.",
                    )}
                </p>
            )}
            {display?.kind === "page" && (
                <>
                    <ol className="master-records">
                        {display.entries.map((e) => (
                            <li key={e.version}>
                                <strong>
                                    {t("版本", "Version")} {e.version} · {action(e.action)}
                                </strong>
                                <span>{e.created_at}</span>
                                <span>
                                    {e.reason ??
                                        t(
                                            "原始确认时没有修订原因。",
                                            "Original confirmation has no revision reason.",
                                        )}
                                </span>
                                <button type="button" onClick={() => setSelected({ ...e })}>
                                    {t("查看固定详情", "View fixed details")}
                                </button>
                            </li>
                        ))}
                    </ol>
                    {!display.entries.length && (
                        <p>{t("此页没有历史。", "No history on this page.")}</p>
                    )}
                    <div className="form-actions">
                        <button
                            type="button"
                            disabled={offset === 0}
                            onClick={() => page(offset - 25)}
                        >
                            {t("上一页", "Previous page")}
                        </button>
                        <button
                            type="button"
                            disabled={
                                display.entries.length < 25 ||
                                display.entries.at(-1)?.action === "cancel" ||
                                offset >= 100000
                            }
                            onClick={() => page(offset + 25)}
                        >
                            {t("下一页", "Next page")}
                        </button>
                    </div>
                </>
            )}
            {display?.kind === "original" && (
                <>
                    <p>
                        {t(
                            "以下为首次确认的固定单据，包含当时全部零金额行和原始凭证。",
                            "This is the fixed original confirmation, including all zero lines and original journals.",
                        )}
                    </p>
                    <BusinessDocumentCurrentFacts
                        current={display.current}
                        locale={locale}
                        historical
                    />
                </>
            )}
            {display?.kind === "revision" && (
                <>
                    <dl className="confirmation-facts">
                        <dt>{t("修订记录", "Revision record")}</dt>
                        <dd>{display.receipt.revision_id}</dd>
                        <dt>{t("版本变化", "Version change")}</dt>
                        <dd>
                            {display.receipt.previous_version} → {display.receipt.version}
                        </dd>
                        <dt>{t("原因", "Reason")}</dt>
                        <dd>{display.receipt.reason}</dd>
                    </dl>
                    <BusinessDocumentRevisionFacts
                        value={display.receipt.previous}
                        locale={locale}
                        title={t("固定前版事实", "Fixed previous facts")}
                    />
                    <section aria-label={t("完整冲销凭证", "Complete reversal journals")}>
                        <h4>{t("完整冲销凭证", "Complete reversal journals")}</h4>
                        <ol className="master-records">
                            {display.receipt.reversals.map((r) => (
                                <li key={r.source_line_id}>
                                    {r.source_line_id} · {r.journal_id}
                                </li>
                            ))}
                        </ol>
                    </section>
                    {display.receipt.replacement ? (
                        <BusinessDocumentRevisionFacts
                            value={display.receipt.replacement}
                            locale={locale}
                            title={t("固定替代事实", "Fixed replacement facts")}
                        />
                    ) : (
                        <p>
                            {t(
                                "此取消没有替代单据。",
                                "This cancellation has no replacement document.",
                            )}
                        </p>
                    )}
                </>
            )}
            <div className="form-actions">
                {selected && (
                    <button type="button" onClick={() => setSelected(null)}>
                        {t("返回历史列表", "Back to history list")}
                    </button>
                )}
                <button type="button" onClick={() => setRefresh((v) => v + 1)}>
                    {t("重新读取历史", "Reload history")}
                </button>
                <button type="button" onClick={onBack}>
                    {t("返回当前单据", "Back to current document")}
                </button>
            </div>
        </section>
    );
}
