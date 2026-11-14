import { useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import type { Locale } from "./i18n";
import type { Asset } from "./ledger-api";
import { getRefundCurrent, getRefundRevision, listRefundHistory } from "./refund-api";
import type {
    RefundCurrent,
    RefundDetail,
    RefundHistoryEntry,
    RefundRevisionReceipt,
} from "./refund-api";
import {
    validRefundCurrent,
    validRefundHistoryPage,
    validRefundRevisionDetail,
} from "./refund-revision-read";
import { RefundFactView } from "./BusinessRefundRevisionResult";
import { settlementError } from "./settlement-errors";

type View =
    | { mode: "current" }
    | { mode: "page"; offset: number }
    | { mode: "revision"; offset: number; entry: RefundHistoryEntry };
type Loaded =
    | { mode: "current"; row: RefundCurrent }
    | { mode: "page"; rows: RefundHistoryEntry[] }
    | { mode: "revision"; row: RefundRevisionReceipt };
export function BusinessRefundRevisionHistory({
    original,
    assets,
    locale,
    onUnauthorized,
}: {
    original: RefundDetail;
    assets: Asset[];
    locale: Locale;
    onUnauthorized: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [view, setView] = useState<View>({ mode: "current" });
    const [refresh, setRefresh] = useState(0);
    const key = [
        original.ledger_id,
        original.id,
        view.mode,
        view.mode === "current" ? 0 : view.offset,
        view.mode === "revision" ? view.entry.revision_id : "",
        refresh,
    ].join("/");
    const [loaded, setLoaded] = useState<{ key: string; value: Loaded } | null>(null);
    const [error, setError] = useState<{ key: string; value: unknown } | null>(null);
    const unauthorized = useRef(onUnauthorized);
    unauthorized.current = onUnauthorized;
    useEffect(() => {
        const controller = new AbortController(),
            signal = controller.signal;
        const ledger = original.ledger_id,
            refund = original.id;
        const scope = {
            ledger_id: ledger,
            refund_id: refund,
            payment_id: original.payment_id,
            operation_id: original.receipt.operation_id,
            payment_asset_id: original.payment_asset_id,
        };
        async function load(): Promise<Loaded> {
            if (view.mode === "current") {
                const row = await getRefundCurrent(ledger, refund, signal);
                if (
                    !validRefundCurrent(
                        row,
                        scope,
                        original.party_id,
                        original.payment_direction,
                        assets,
                    )
                )
                    throw new ApiError("server", 200, "invalid_response");
                return { mode: "current", row };
            }
            if (view.mode === "page") {
                const rows = await listRefundHistory(ledger, refund, view.offset, signal);
                if (
                    !validRefundHistoryPage(rows, view.offset) ||
                    (view.offset === 0 && rows[0].reason !== original.reason)
                )
                    throw new ApiError("server", 200, "invalid_response");
                return { mode: "page", rows };
            }
            if (!view.entry.revision_id) throw new ApiError("server", 200, "invalid_response");
            const row = await getRefundRevision(ledger, refund, view.entry.revision_id, signal);
            if (!validRefundRevisionDetail(row, scope, view.entry, assets))
                throw new ApiError("server", 200, "invalid_response");
            return { mode: "revision", row };
        }
        load().then(
            (value) => {
                if (!signal.aborted) {
                    setLoaded({ key, value });
                    setError(null);
                }
            },
            (caught) => {
                if (signal.aborted) return;
                setError({ key, value: caught });
                if (caught instanceof ApiError && caught.status === 401) unauthorized.current();
            },
        );
        return () => controller.abort();
    }, [original, assets, view, refresh, key]);
    const value = loaded?.key === key ? loaded.value : null;
    return (
        <section
            className="business-settlement-panel"
            aria-label={t("退款版本", "Refund versions")}
        >
            <div className="form-actions">
                <button
                    type="button"
                    aria-pressed={view.mode === "current"}
                    onClick={() => setView({ mode: "current" })}
                >
                    {t("当前退款", "Current refund")}
                </button>
                <button
                    type="button"
                    aria-pressed={view.mode !== "current"}
                    onClick={() => setView({ mode: "page", offset: 0 })}
                >
                    {t("版本历史", "Version history")}
                </button>
            </div>
            {error?.key === key ? (
                <div role="alert">
                    {settlementError(error.value, locale)}{" "}
                    <button type="button" onClick={() => setRefresh((x) => x + 1)}>
                        {t("重新读取版本", "Reload version")}
                    </button>
                </div>
            ) : (
                !value && <p role="status">{t("读取退款版本…", "Loading refund version…")}</p>
            )}
            {value?.mode === "current" && (
                <>
                    <h4>
                        {t("当前版本", "Current version")} {value.row.version} ·{" "}
                        {value.row.status === "active"
                            ? t("有效", "Active")
                            : t("已取消", "Cancelled")}
                    </h4>
                    {value.row.revision_reason && <p>{value.row.revision_reason}</p>}
                    {value.row.fact ? (
                        <RefundFactView assets={assets} locale={locale} fact={value.row.fact} />
                    ) : (
                        <p>
                            {t(
                                "当前已无有效退款本金。历史凭证和回执可在版本历史中查阅。",
                                "No current refund principal. Previous postings and receipts are available in version history.",
                            )}
                        </p>
                    )}
                </>
            )}
            {value?.mode === "page" && view.mode === "page" && (
                <>
                    <ol className="master-records" start={view.offset + 1}>
                        {value.rows.map((row) => (
                            <li key={row.version}>
                                <strong>
                                    {t("版本", "Version")} {row.version} ·{" "}
                                    {row.action === "create"
                                        ? t("创建", "Created")
                                        : row.action === "correct"
                                          ? t("更正", "Corrected")
                                          : t("取消", "Cancelled")}
                                </strong>
                                <span>{row.reason}</span>
                                <time dateTime={row.created_at}>{row.created_at}</time>
                                {row.revision_id ? (
                                    <button
                                        type="button"
                                        onClick={() =>
                                            setView({
                                                mode: "revision",
                                                offset: view.offset,
                                                entry: row,
                                            })
                                        }
                                    >
                                        {t("查看修订回执", "View revision receipt")}
                                    </button>
                                ) : (
                                    <span>
                                        {t(
                                            "原创建回执见原退款详情。",
                                            "The original receipt is in the original refund details.",
                                        )}
                                    </span>
                                )}
                            </li>
                        ))}
                    </ol>
                    {value.rows.length === 0 && (
                        <p>{t("本页没有版本。", "No versions on this page.")}</p>
                    )}
                    <div className="form-actions">
                        <button
                            type="button"
                            disabled={view.offset === 0}
                            onClick={() => setView({ mode: "page", offset: view.offset - 25 })}
                        >
                            {t("上一页版本", "Previous versions")}
                        </button>
                        <button
                            type="button"
                            disabled={value.rows.length !== 25 || view.offset >= 100000}
                            onClick={() => setView({ mode: "page", offset: view.offset + 25 })}
                        >
                            {t("下一页版本", "Next versions")}
                        </button>
                    </div>
                </>
            )}
            {value?.mode === "revision" && view.mode === "revision" && (
                <>
                    <h4>
                        {t("固定修订回执", "Saved revision receipt")} · {value.row.previous_version}{" "}
                        → {value.row.version}
                    </h4>
                    <p>{value.row.reason}</p>
                    <p>
                        {t("原凭证", "Previous journal")} {value.row.previous_journal_id}
                    </p>
                    <p>
                        {t("冲销凭证", "Reversal journal")} {value.row.reversal_journal_id}
                    </p>
                    {value.row.replacement_journal_id && (
                        <p>
                            {t("替代凭证", "Replacement journal")}{" "}
                            {value.row.replacement_journal_id}
                        </p>
                    )}

                    <h5>{t("修订前", "Before revision")}</h5>
                    <RefundFactView assets={assets} fact={value.row.previous} locale={locale} />
                    {value.row.replacement && (
                        <>
                            <h5>{t("修订后", "After revision")}</h5>
                            <RefundFactView
                                assets={assets}
                                fact={value.row.replacement}
                                locale={locale}
                            />
                        </>
                    )}
                    <button
                        type="button"
                        onClick={() => setView({ mode: "page", offset: view.offset })}
                    >
                        {t("返回此页版本", "Back to version page")}
                    </button>
                </>
            )}
        </section>
    );
}
