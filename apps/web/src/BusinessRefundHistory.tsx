import { useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import { getRefund, listRefunds } from "./refund-api";
import type { RefundDetail, RefundSummary } from "./refund-api";
import { businessError } from "./business-errors";
import { formatAmount, parseAmount } from "./money";
import type { Locale } from "./i18n";

export function BusinessRefundHistory({
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
    const key = `${ledgerId}/${selected ?? `page:${offset}`}/${refresh}`;
    const [loaded, setLoaded] = useState<{
        key: string;
        data: RefundDetail | RefundSummary[];
    } | null>(null);
    const [error, setError] = useState<{ key: string; value: unknown } | null>(null);
    const callback = useRef(onUnauthorized);
    callback.current = onUnauthorized;
    useEffect(() => {
        const abort = new AbortController();
        const task = selected
            ? getRefund(ledgerId, selected, abort.signal)
            : listRefunds(ledgerId, undefined, offset, abort.signal);
        task.then((data) => {
            if (abort.signal.aborted) return;
            const rows = Array.isArray(data) ? data : [data];
            if (
                rows.some(
                    (row) =>
                        row.ledger_id !== ledgerId ||
                        typeof row.reason !== "string" ||
                        parseAmount(row.amount, 18) <= 0n ||
                        parseAmount(row.payment_amount, 18) <= 0n,
                ) ||
                (!Array.isArray(data) &&
                    (data.id !== selected ||
                        data.receipt.id !== data.id ||
                        data.receipt.ledger_id !== ledgerId ||
                        data.receipt.payment_id !== data.payment_id))
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
    }, [ledgerId, selected, offset, key]);
    const data = loaded?.key === key ? loaded.data : null;
    const amount = (value: string, asset: string) => `${formatAmount(value, locale)} ${asset}`;
    return (
        <section
            className="business-settlement-panel"
            aria-label={t("实际退款历史", "Actual refund history")}
        >
            <h2>{t("实际退款历史", "Actual refund history")}</h2>
            <p>
                {t(
                    "此处展示已保存事实；未知提交仍须用原请求重试，不凭余额或相似记录判断成功。",
                    "These are saved facts. Retry an unknown submission using its original request; balances or similar records do not prove success.",
                )}
            </p>
            {error?.key === key && <p role="alert">{businessError(error.value, locale)}</p>}
            {!data && error?.key !== key && <p role="status">{t("读取中…", "Loading…")}</p>}
            {data &&
                (Array.isArray(data) ? (
                    <>
                        {data.length === 0 && (
                            <p>{t("本页无退款。", "No refunds on this page.")}</p>
                        )}
                        <ul className="master-records">
                            {data.map((row) => (
                                <li key={row.id}>
                                    <strong>
                                        {row.transaction_date} · {amount(row.amount, row.asset_id)}
                                    </strong>
                                    <span>{row.reason}</span>
                                    <button type="button" onClick={() => setSelected(row.id)}>
                                        {t("查看退款详情", "View refund details")}
                                    </button>
                                </li>
                            ))}
                        </ul>
                        <div className="form-actions">
                            <button
                                type="button"
                                disabled={offset === 0}
                                onClick={() => setOffset((value) => value - 25)}
                            >
                                {t("上一页", "Previous page")}
                            </button>
                            <button
                                type="button"
                                disabled={data.length !== 25}
                                onClick={() => setOffset((value) => value + 25)}
                            >
                                {t("下一页", "Next page")}
                            </button>
                        </div>
                    </>
                ) : (
                    <>
                        <dl className="confirmation-facts">
                            <dt>{t("原付款标识", "Original payment ID")}</dt>
                            <dd>{data.payment_id}</dd>
                            <dt>{t("退款方向", "Refund direction")}</dt>
                            <dd>
                                {data.payment_direction === "receipt"
                                    ? t("退给客户，资金减少", "Return to customer; cash decreases")
                                    : t(
                                          "供应商退款，资金增加",
                                          "Refund from supplier; cash increases",
                                      )}
                            </dd>
                            <dt>{t("实际退款数量", "Actual refund quantity")}</dt>
                            <dd>{amount(data.amount, data.asset_id)}</dd>
                            <dt>{t("退还原付款本金", "Original principal being refunded")}</dt>
                            <dd>{amount(data.payment_amount, data.payment_asset_id)}</dd>
                            <dt>{t("退款交易日", "Refund transaction date")}</dt>
                            <dd>{data.transaction_date}</dd>
                            <dt>{t("退款原因", "Refund reason")}</dt>
                            <dd>{data.reason}</dd>
                        </dl>
                        <h3>{t("独立手续费", "Separate fees")}</h3>
                        {data.receipt.fees.length === 0 ? (
                            <p>{t("无手续费。", "No fees.")}</p>
                        ) : (
                            <ul className="master-records">
                                {data.receipt.fees.map((fee, index) => (
                                    <li key={index}>
                                        <strong>{amount(fee.amount, fee.asset_id)}</strong>
                                        <span>
                                            {fee.account_id} · {fee.category_id}
                                        </span>
                                    </li>
                                ))}
                            </ul>
                        )}
                        <details>
                            <summary>
                                {t("原回执与来源标识", "Original receipt and source references")}
                            </summary>
                            <p>{data.id}</p>
                            <p>{data.receipt.operation_id}</p>
                            <p>{data.receipt.journal_id}</p>
                            <ul>
                                {data.receipt.allocation_return_ids.map((id) => (
                                    <li key={id}>{id}</li>
                                ))}
                            </ul>
                        </details>
                        <button type="button" onClick={() => setSelected(null)}>
                            {t("返回退款列表", "Back to refund list")}
                        </button>
                    </>
                ))}
            <div className="form-actions">
                <button type="button" onClick={() => setRefresh((value) => value + 1)}>
                    {t("重新读取", "Reload")}
                </button>
                <button type="button" onClick={onClose}>
                    {t("返回收付列表", "Back to settlement list")}
                </button>
            </div>
        </section>
    );
}
