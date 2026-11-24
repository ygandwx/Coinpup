import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import type { Locale } from "./i18n";
import { listInvoiceHistory, getInvoiceIssuance } from "./invoice-api";
import { validListedInvoice } from "./invoice-issuance-receipt";
import type { InvoicePage, InvoiceRow, InvoiceState } from "./invoice-issuance-receipt";
import "./invoice-panel.css";
import { InvoiceOriginals } from "./InvoiceOriginals";

export function InvoiceHistoryPanel({
    ledgerId,
    locale,
    session,
    onUnauthorized,
}: {
    ledgerId: string;
    locale: Locale;
    session: Session;
    onUnauthorized: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [pages, setPages] = useState<(string | null)[]>([null]),
        [selected, setSelected] = useState<InvoiceRow | null>(null),
        [reload, setReload] = useState(0);
    const cursor = pages.at(-1) ?? null;
    const req = useMemo(
        () => ({ session, ledgerId, cursor, selected, reload }),
        [ledgerId, session, cursor, selected, reload],
    );
    const current = useRef({ req, onUnauthorized });
    current.current = { req, onUnauthorized };
    const [loaded, setLoaded] = useState<{
        req: typeof req;
        page?: InvoicePage;
        detail?: InvoiceState;
        failed?: boolean;
    } | null>(null);
    useEffect(() => {
        const a = new AbortController();
        const relevant = () => !a.signal.aborted && current.current.req === req;
        async function read() {
            if (req.selected) {
                const d = await getInvoiceIssuance(req.ledgerId, req.selected.id, a.signal);
                if (!relevant()) return;
                if (!validListedInvoice(d, req.selected) || d.ledger_id !== req.ledgerId)
                    throw new ApiError("server", 200, "invalid_response");
                setLoaded({ req, detail: d });
            } else {
                const page = await listInvoiceHistory(req.ledgerId, null, req.cursor, a.signal);
                if (relevant()) setLoaded({ req, page });
            }
        }
        void read().catch((e) => {
            if (relevant()) {
                setLoaded({ req, failed: true });
                if (e instanceof ApiError && e.kind === "unauthorized")
                    current.current.onUnauthorized();
            }
        });
        return () => a.abort();
    }, [req]);
    const loadedHere = loaded?.req === req ? loaded : null;
    const status = (r: InvoiceRow) =>
        r.document_status === "cancelled"
            ? t("单据已取消", "Document cancelled")
            : r.document_status === "superseded"
              ? t("单据已更正", "Document corrected")
              : t("当前单据版本", "Current document version");
    const issuance = (s: InvoiceState["state"]) =>
        s === "pending"
            ? t("号码已预留，原件尚未封存", "Number reserved; originals pending")
            : t("已签发两份原件", "Both originals issued");
    return (
        <section
            className="business-panel invoice-panel"
            aria-label={t("签发历史", "Issuance history")}
        >
            <h3>{t("签发历史", "Issuance history")}</h3>
            {loadedHere?.failed ? (
                <p role="alert">
                    {t("无法核对这份签发记录。", "This issuance record could not be verified.")}
                </p>
            ) : !loadedHere ? (
                <p role="status">{t("正在读取签发记录…", "Loading issuance records…")}</p>
            ) : null}
            {loadedHere?.page && (
                <>
                    {loadedHere.page.items.length === 0 && (
                        <p>{t("没有签发记录。", "No issuance records.")}</p>
                    )}
                    <ul>
                        {loadedHere.page.items.map((r) => (
                            <li key={r.id}>
                                <button
                                    className="secondary-button"
                                    type="button"
                                    onClick={() => setSelected(r)}
                                >
                                    {r.number}
                                </button>
                                {" · "}
                                {issuance(r.state)}
                                {" · "}
                                {status(r)}
                                {" · "}
                                {t("版本", "Version")} {r.document_version}
                            </li>
                        ))}
                    </ul>
                    <button
                        className="secondary-button"
                        type="button"
                        disabled={pages.length <= 1}
                        onClick={() => setPages((p) => p.slice(0, -1))}
                    >
                        {t("上一页", "Previous page")}
                    </button>
                    <button
                        className="secondary-button"
                        type="button"
                        disabled={loadedHere.page.next_before === null}
                        onClick={() => {
                            const next = loadedHere.page?.next_before;
                            if (next) setPages((p) => [...p, next]);
                        }}
                    >
                        {t("下一页", "Next page")}
                    </button>
                </>
            )}
            {selected && loadedHere?.detail && (
                <article>
                    <h4>{loadedHere.detail.number}</h4>
                    <p>{issuance(loadedHere.detail.state)}</p>
                    <p>
                        {status(selected)} · {t("签发版本", "Issued version")}{" "}
                        {selected.document_version} · {t("当前版本", "Current version")}{" "}
                        {selected.current_document_version}
                    </p>
                    <p>
                        {t("单据", "Document")}: {selected.document_id}
                    </p>
                    <p>
                        {t("签发记录", "Issuance")}: {selected.id}
                    </p>
                    {selected.previous_issuance_id && (
                        <p>
                            {t("前次签发", "Previous issuance")}: {selected.previous_issuance_id}
                        </p>
                    )}
                    {loadedHere.detail.state === "complete" && (
                        <InvoiceOriginals
                            key={loadedHere.detail.id}
                            ledgerId={ledgerId}
                            result={loadedHere.detail}
                            locale={locale}
                            onUnauthorized={onUnauthorized}
                        />
                    )}
                </article>
            )}
            {selected && (
                <button
                    className="secondary-button"
                    type="button"
                    onClick={() => setSelected(null)}
                >
                    {t("返回记录列表", "Back to records")}
                </button>
            )}
            <button
                className="secondary-button"
                type="button"
                onClick={() => {
                    setSelected(null);
                    setPages([null]);
                    setReload((v) => v + 1);
                }}
            >
                {t("刷新签发历史", "Refresh issuance history")}
            </button>
        </section>
    );
}
