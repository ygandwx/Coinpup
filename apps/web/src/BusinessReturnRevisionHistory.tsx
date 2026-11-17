import { BusinessReturnRevisionEditor } from "./BusinessReturnRevisionEditor";
import type { ReturnRevisionActions } from "./BusinessReturnRevisionEditor";
import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "./api";
import type { Locale } from "./i18n";
import type { AssetScales } from "./settlement-revision-facts";
import type { DeepReadonly } from "./settlement-intent";
import type { Asset } from "./ledger-api";
import { amountFromMinorUnits, formatAmount } from "./money";
import type { AllocationReturnDetail } from "./allocation-return-api";
import { currentReturn, returnHistory, returnRevision } from "./allocation-return-revision-api";
import type {
    ReturnCurrent,
    ReturnHistoryEntry,
    ReturnRevisionReceipt,
} from "./allocation-return-revision-api";
import { validReturnCurrent, validReturnRevision } from "./allocation-return-revision-facts";
import { storedUnits, validHistoryPage } from "./settlement-revision-facts";
import { settlementError } from "./settlement-errors";

type View =
    | { mode: "current" }
    | { mode: "history"; offset: number }
    | { mode: "revision"; offset: number; entry: ReturnHistoryEntry };
type Data =
    | { mode: "current"; current: ReturnCurrent }
    | { mode: "history"; page: ReturnHistoryEntry[] }
    | { mode: "revision"; receipt: ReturnRevisionReceipt };

function displayQuantity(
    value: string,
    asset: string,
    assets: AssetScales,
    locale: Locale,
): string {
    const definition = assets.find((a) => a.asset_id === asset);
    if (!definition) return formatAmount(value, locale) + " " + asset;
    const units = storedUnits(value, asset, assets);
    return formatAmount(amountFromMinorUnits(units, definition.scale), locale) + " " + asset;
}

export function ReturnRevisionFacts({
    receipt,
    assets,
    locale,
}: {
    receipt: DeepReadonly<ReturnRevisionReceipt>;
    assets: AssetScales;
    locale: Locale;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const show = (text: string, asset: string) => displayQuantity(text, asset, assets, locale);
    return (
        <>
            <h4>
                {t("固定修订回执", "Saved revision receipt")} · {receipt.previous_version} →{" "}
                {receipt.version}
            </h4>
            <p>{receipt.reason}</p>
            <dl className="confirmation-facts">
                <dt>{t("此前退回", "Previous return")}</dt>
                <dd>
                    {receipt.previous.return_date} · {receipt.previous.reason}
                </dd>
                {receipt.replacement && (
                    <>
                        <dt>{t("替代退回", "Replacement return")}</dt>
                        <dd>
                            {receipt.replacement.return_date} · {receipt.replacement.reason}
                        </dd>
                    </>
                )}
            </dl>
            <ol>
                {receipt.members.map((m) => (
                    <li key={m.operation_id}>
                        <strong>
                            {m.action === "cancel"
                                ? t("移除来源", "Removed source")
                                : m.action === "create"
                                  ? t("新加入来源", "Added source")
                                  : t("保留来源", "Retained source")}
                        </strong>
                        <p>
                            {t("来源序位", "Source position")} {m.source_return_no} ·{" "}
                            {m.source_allocation_line_id}
                        </p>
                        {m.previous && (
                            <p>
                                {t("此前两侧数量", "Previous quantities")} ·{" "}
                                {show(m.previous.payment_amount, receipt.payment_asset_id)} /{" "}
                                {show(m.previous.document_amount, m.document_asset_id)}
                            </p>
                        )}
                        {m.replacement && (
                            <p>
                                {t("替代两侧数量", "Replacement quantities")} ·{" "}
                                {show(m.replacement.payment_amount, receipt.payment_asset_id)} /{" "}
                                {show(m.replacement.document_amount, m.document_asset_id)}
                            </p>
                        )}
                        <details>
                            <summary>{t("操作与凭证", "Operation and journals")}</summary>
                            <p>
                                {t("成员操作", "Member operation")} · {m.operation_id} · v
                                {m.operation_version}
                            </p>
                            {m.previous && (
                                <p>
                                    {t("此前凭证", "Previous journal")} · {m.previous.journal_id}
                                </p>
                            )}
                            {m.reversal_journal_id && (
                                <p>
                                    {t("冲销凭证", "Reversal journal")} · {m.reversal_journal_id}
                                </p>
                            )}
                            {m.replacement && (
                                <p>
                                    {t("替代凭证", "Replacement journal")} ·{" "}
                                    {m.replacement.journal_id}
                                </p>
                            )}
                        </details>
                    </li>
                ))}
            </ol>
        </>
    );
}

export function BusinessReturnRevisionHistory({
    original,
    actions,
    assets,
    locale,
    onUnauthorized,
}: {
    original: AllocationReturnDetail;
    actions: ReturnRevisionActions;
    assets: Asset[];
    locale: Locale;
    onUnauthorized: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [view, setView] = useState<View>({ mode: "current" });
    const [refresh, setRefresh] = useState(0);
    // Selecting the same view again starts a new read of the current facts.
    const key = useMemo(
        () => ({ original, assets, view, refresh }),
        [original, assets, view, refresh],
    );
    const [loaded, setLoaded] = useState<{ key: typeof key; data: Data } | null>(null);
    const [error, setError] = useState<{ key: typeof key; cause: unknown } | null>(null);
    const unauthorized = useRef(onUnauthorized);
    unauthorized.current = onUnauthorized;
    useEffect(() => {
        const controller = new AbortController(),
            signal = controller.signal;
        const { original, assets, view } = key;
        const { ledger_id: ledger, id, payment_id, asset_id } = original;
        const scope = { ledger_id: ledger, return_id: id, payment_id, payment_asset_id: asset_id };
        async function load(): Promise<Data> {
            if (view.mode === "current") {
                const row = await currentReturn(ledger, id, signal);
                if (validReturnCurrent(row, scope, assets))
                    return { mode: "current", current: row };
            } else if (view.mode === "history") {
                const rows = await returnHistory(ledger, id, view.offset, signal);
                if (validHistoryPage(rows, view.offset)) return { mode: "history", page: rows };
            } else if (view.entry.revision_id) {
                const row = await returnRevision(ledger, id, view.entry.revision_id, signal);
                if (validReturnRevision(row, scope, view.entry, assets))
                    return { mode: "revision", receipt: row };
            }
            throw new ApiError("server", 200, "invalid_response");
        }
        load()
            .then((data) => {
                if (!signal.aborted) {
                    setLoaded({ key, data });
                    setError(null);
                }
            })
            .catch((cause) => {
                if (signal.aborted) return;
                if (cause instanceof ApiError && cause.status === 401) unauthorized.current();
                else setError({ key, cause });
            });
        return () => controller.abort();
    }, [key]);
    const data = loaded?.key === key ? loaded.data : null;
    return (
        <section
            className="business-settlement-panel return-versions"
            aria-label={t("退回组版本", "Return group versions")}
        >
            <h3>{t("退回组版本", "Return group versions")}</h3>
            <div className="form-actions">
                <button type="button" onClick={() => setView({ mode: "current" })}>
                    {t("当前退回组", "Current return group")}
                </button>
                <button type="button" onClick={() => setView({ mode: "history", offset: 0 })}>
                    {t("退回版本目录", "Return version history")}
                </button>
                <button type="button" onClick={() => setRefresh((n) => n + 1)}>
                    {t("刷新版本", "Reload versions")}
                </button>
            </div>
            {error?.key === key ? (
                <p role="alert">{settlementError(error.cause, locale)}</p>
            ) : (
                !data && <p role="status">{t("正在读取版本…", "Loading versions…")}</p>
            )}
            {data?.mode === "current" && (
                <>
                    <h4>
                        {t("组版本", "Group version")} {data.current.version} ·{" "}
                        {data.current.status === "active"
                            ? t("有效", "Active")
                            : t("已取消", "Cancelled")}
                    </h4>
                    {data.current.fact && (
                        <p>
                            {data.current.fact.return_date} · {data.current.fact.reason}
                        </p>
                    )}
                    {data.current.revision_reason && (
                        <p>
                            {t("修订原因", "Revision reason")} · {data.current.revision_reason}
                        </p>
                    )}
                    <ol>
                        {data.current.members.map((m) => (
                            <li key={m.operation_id}>
                                <p>
                                    {displayQuantity(
                                        m.payment_amount,
                                        data.current.payment_asset_id,
                                        assets,
                                        locale,
                                    )}{" "}
                                    /{" "}
                                    {displayQuantity(
                                        m.document_amount,
                                        m.document_asset_id,
                                        assets,
                                        locale,
                                    )}
                                </p>
                                <details>
                                    <summary>
                                        {t("当前来源与凭证", "Current source and journal")}
                                    </summary>
                                    <p>
                                        {t("来源序位", "Source position")} {m.source_return_no} ·{" "}
                                        {m.source_allocation_line_id}
                                    </p>
                                    <p>
                                        {m.document_id} / {m.document_line_id}
                                    </p>
                                    <p>
                                        {m.operation_id} · v{m.operation_version} · {m.journal_id}
                                    </p>
                                </details>
                            </li>
                        ))}
                    </ol>
                    {data.current.status === "active" && error?.key !== key && (
                        <BusinessReturnRevisionEditor
                            key={data.current.return_id + "/" + data.current.version}
                            original={original}
                            current={data.current}
                            assets={assets}
                            locale={locale}
                            actions={actions}
                            onUnauthorized={onUnauthorized}
                        />
                    )}
                </>
            )}
            {data?.mode === "history" && view.mode === "history" && (
                <>
                    <ol start={view.offset + 1}>
                        {data.page.map((entry) => (
                            <li key={entry.version}>
                                <p>
                                    {entry.action === "create"
                                        ? t("原始创建", "Original creation")
                                        : entry.action === "correct"
                                          ? t("更正", "Correction")
                                          : t("取消", "Cancellation")}{" "}
                                    · {entry.reason}
                                </p>
                                {entry.revision_id && (
                                    <button
                                        type="button"
                                        onClick={() =>
                                            setView({
                                                mode: "revision",
                                                entry,
                                                offset: view.offset,
                                            })
                                        }
                                    >
                                        {t("查看版本", "View version")} {entry.version}
                                    </button>
                                )}
                            </li>
                        ))}
                    </ol>
                    {data.page.length === 0 && (
                        <p>{t("本页没有版本。", "No versions on this page.")}</p>
                    )}
                    <div className="form-actions">
                        <button
                            type="button"
                            disabled={view.offset === 0}
                            onClick={() => setView({ mode: "history", offset: view.offset - 25 })}
                        >
                            {t("上一页版本", "Previous versions")}
                        </button>
                        <button
                            type="button"
                            disabled={data.page.length < 25 || view.offset >= 100000}
                            onClick={() => setView({ mode: "history", offset: view.offset + 25 })}
                        >
                            {t("下一页版本", "Next versions")}
                        </button>
                    </div>
                </>
            )}
            {data?.mode === "revision" && view.mode === "revision" && (
                <>
                    <ReturnRevisionFacts receipt={data.receipt} assets={assets} locale={locale} />
                    <button
                        type="button"
                        onClick={() => setView({ mode: "history", offset: view.offset })}
                    >
                        {t("返回此页版本", "Back to version page")}
                    </button>
                </>
            )}
        </section>
    );
}
