import { useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import { getAllocationReturn, listAllocationReturns } from "./allocation-return-api";
import type { AllocationReturnDetail, AllocationReturnSummary } from "./allocation-return-api";
import { businessError } from "./business-errors";
import { formatAmount } from "./money";
import type { Locale } from "./i18n";

export function BusinessAllocationReturnHistory({
    ledgerId,
    locale,
    onClose,
    onUnauthorized,
}: {
    ledgerId: string;
    locale: Locale;
    onClose: () => void;
    onUnauthorized: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [selected, setSelected] = useState<string | null>(null),
        [offset, setOffset] = useState(0);
    const [refresh, setRefresh] = useState(0);
    const key = `${ledgerId}/${selected ?? `page:${offset}`}`;
    const [loaded, setLoaded] = useState<{
        key: string;
        data: AllocationReturnDetail | AllocationReturnSummary[];
    } | null>(null);
    const [error, setError] = useState<{ key: string; value: unknown } | null>(null);
    const callback = useRef(onUnauthorized);
    callback.current = onUnauthorized;
    useEffect(() => {
        const abort = new AbortController();
        const task = selected
            ? getAllocationReturn(ledgerId, selected, abort.signal)
            : listAllocationReturns(ledgerId, undefined, offset, abort.signal);
        task.then((data) => {
            if (abort.signal.aborted) return;
            if (
                Array.isArray(data)
                    ? data.some((row) => row.ledger_id !== ledgerId)
                    : data.ledger_id !== ledgerId ||
                      data.id !== selected ||
                      !Array.isArray(data.lines)
            )
                throw new ApiError("server", 200, "invalid_response");
            setLoaded({ key, data });
            setError(null);
        }).catch((caught) => {
            if (abort.signal.aborted) return;
            if (caught instanceof ApiError && caught.status === 401) callback.current();
            else setError({ key, value: caught });
        });
        return () => abort.abort();
    }, [ledgerId, selected, offset, key, refresh]);
    const data = loaded?.key === key ? loaded.data : null;
    return (
        <section
            className="business-settlement-panel"
            aria-label={t("分摊退回历史", "Allocation return history")}
        >
            <h2>{t("分摊退回历史", "Allocation return history")}</h2>
            {error?.key === key && <p role="alert">{businessError(error.value, locale)}</p>}
            {!data && error?.key !== key && <p role="status">{t("读取中…", "Loading…")}</p>}
            {data &&
                (Array.isArray(data) ? (
                    <>
                        <ul className="master-records">
                            {data.map((row) => (
                                <li key={row.id}>
                                    <strong>
                                        {row.return_date} ·{" "}
                                        {formatAmount(row.payment_amount, locale)} {row.asset_id}
                                    </strong>
                                    <p>{row.reason}</p>
                                    <button type="button" onClick={() => setSelected(row.id)}>
                                        {t("查看退回详情", "View return details")}
                                    </button>
                                </li>
                            ))}
                        </ul>
                        {data.length === 0 && (
                            <p>{t("此页暂无已保存的退回。", "No saved returns on this page.")}</p>
                        )}
                        <div className="form-actions">
                            <button
                                type="button"
                                disabled={offset === 0}
                                onClick={() => setOffset((n) => n - 25)}
                            >
                                {t("上一页", "Previous page")}
                            </button>
                            <button
                                type="button"
                                disabled={data.length !== 25 || offset >= 100000}
                                onClick={() => setOffset((n) => n + 25)}
                            >
                                {t("下一页", "Next page")}
                            </button>
                        </div>
                    </>
                ) : (
                    <>
                        <dl className="confirmation-facts">
                            <dt>{t("退回生效日", "Return effective date")}</dt>
                            <dd>{data.return_date}</dd>
                            <dt>{t("退回原因", "Return reason")}</dt>
                            <dd>{data.reason}</dd>
                        </dl>
                        <ol className="confirmation-lines">
                            {data.lines.map((line) => (
                                <li key={line.line_no}>
                                    <strong>
                                        {line.description || t("单据明细", "Document line")}
                                    </strong>
                                    <p>
                                        {t("付款侧退回", "Payment amount returned")}:{" "}
                                        {formatAmount(line.payment_amount, locale)} {data.asset_id}
                                    </p>
                                    <p>
                                        {t("单据侧退回", "Document amount returned")}:{" "}
                                        {formatAmount(line.document_amount, locale)}{" "}
                                        {line.document_asset_id}
                                    </p>
                                    <details>
                                        <summary>
                                            {t("来源与回执标识", "Source and receipt references")}
                                        </summary>
                                        <p>
                                            {line.source_allocation_id} /{" "}
                                            {line.source_allocation_line_id}
                                        </p>
                                        <p>
                                            {line.document_id} / {line.document_line_id}
                                        </p>
                                        <p>
                                            {line.operation_id} / {line.journal_id}
                                        </p>
                                    </details>
                                </li>
                            ))}
                        </ol>
                        <button type="button" onClick={() => setSelected(null)}>
                            {t("返回退回列表", "Back to return list")}
                        </button>
                    </>
                ))}
            <div className="form-actions">
                <button type="button" onClick={() => setRefresh((n) => n + 1)}>
                    {t("重新读取", "Reload")}
                </button>
                <button type="button" onClick={onClose}>
                    {t("返回收付列表", "Back to settlement list")}
                </button>
            </div>
        </section>
    );
}
