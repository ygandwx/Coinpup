import { useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import type { Locale } from "./i18n";
import type { Account } from "./ledger-api";
import { businessError } from "./business-errors";
import { getMasterData } from "./business-api";
import {
    getBusinessAllocation,
    getBusinessPayment,
    listBusinessAllocations,
    listBusinessPayments,
} from "./business-settlement-api";
import type {
    BusinessAllocationDetail,
    BusinessAllocationSummary,
    BusinessPaymentDetail,
    BusinessPaymentSummary,
} from "./business-settlement-api";
import { formatAmount } from "./money";

type Mode = "payments" | "allocations";
type Summary = BusinessPaymentSummary | BusinessAllocationSummary;
type Detail = BusinessPaymentDetail | BusinessAllocationDetail;
export function BusinessSettlementHistory({
    ledgerId,
    locale,
    accounts,
    onEditing,
    onAllocate,
    onUnauthorized,
}: {
    ledgerId: string;
    locale: Locale;
    accounts: Account[];
    onEditing: (value: boolean) => void;
    onAllocate?: (payment: BusinessPaymentDetail, name: string | null) => void;
    onUnauthorized: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const amount = (value: string, asset: string) => `${formatAmount(value, locale)} ${asset}`;
    const [mode, setMode] = useState<Mode>("payments");
    const [offset, setOffset] = useState(0),
        [refresh, setRefresh] = useState(0);
    const [paymentFilter, setPaymentFilter] = useState<string | undefined>();
    const [result, setResult] = useState<{ key: string; rows: Summary[] } | null>(null);
    const [failure, setFailure] = useState<{
        key: string;
        error: unknown;
    } | null>(null);
    const [target, setTarget] = useState<string | null>(null);
    const [detail, setDetail] = useState<Detail | null>(null);
    const [partyName, setPartyName] = useState<string | null>(null);
    const [error, setError] = useState<unknown>(null);
    const callbacks = useRef({ onEditing, onUnauthorized });
    callbacks.current = { onEditing, onUnauthorized };
    const detailAbort = useRef<AbortController | null>(null);
    const key = JSON.stringify([ledgerId, mode, paymentFilter, offset, refresh]);
    const rows = result?.key === key ? result.rows : null;
    const readError = failure?.key === key ? failure.error : null;
    useEffect(() => {
        callbacks.current.onEditing(target !== null);
        return () => callbacks.current.onEditing(false);
    }, [target]);
    useEffect(() => () => detailAbort.current?.abort(), []);
    useEffect(() => {
        const abort = new AbortController();
        const read =
            mode === "payments"
                ? listBusinessPayments(ledgerId, offset, abort.signal)
                : listBusinessAllocations(ledgerId, paymentFilter, offset, abort.signal);
        read.then((items) => {
            if (abort.signal.aborted) return;
            if (items.some((row) => row.ledger_id !== ledgerId))
                throw new ApiError("server", 200, "invalid_response");
            setResult({ key, rows: items });
        }).catch((caught) => {
            if (abort.signal.aborted) return;
            if (caught instanceof ApiError && caught.status === 401)
                callbacks.current.onUnauthorized();
            else setFailure({ key, error: caught });
        });
        return () => abort.abort();
    }, [ledgerId, mode, paymentFilter, offset, key]);
    async function read(id: string) {
        if (target) return;
        detailAbort.current?.abort();
        const abort = new AbortController();
        detailAbort.current = abort;
        setTarget(id);
        setDetail(null);
        setPartyName(null);
        setError(null);
        try {
            const value =
                mode === "payments"
                    ? await getBusinessPayment(ledgerId, id, abort.signal)
                    : await getBusinessAllocation(ledgerId, id, abort.signal);
            if (abort.signal.aborted) return;
            if (value.id !== id || value.ledger_id !== ledgerId)
                throw new ApiError("server", 200, "invalid_response");
            setDetail(value);
            if ("party_id" in value) {
                const party = await getMasterData(
                    ledgerId,
                    "parties",
                    value.party_id,
                    abort.signal,
                );
                if (
                    !abort.signal.aborted &&
                    party.id === value.party_id &&
                    party.ledger_id === ledgerId
                )
                    setPartyName(party.name);
            }
        } catch (caught) {
            if (abort.signal.aborted) return;
            if (caught instanceof ApiError && caught.status === 401)
                callbacks.current.onUnauthorized();
            else setError(caught);
        }
    }
    function close() {
        detailAbort.current?.abort();
        setTarget(null);
        setDetail(null);
        setPartyName(null);
        setError(null);
    }
    const account = (id: string) => accounts.find((row) => row.id === id)?.name ?? id;
    return (
        <section
            className="business-panel business-settlement-panel"
            aria-label={t("经营收付与分摊", "Business payments and allocations")}
        >
            <h2>{t("经营收付与分摊", "Business payments and allocations")}</h2>
            <p className="help-text">
                {t(
                    "收付款记录实际资金变化；分摊使用已收付金额结清单据，不再扣款。金额按各自原币显示。",
                    "Payments record actual money movements. Allocations settle documents using those payments without another charge. Amounts retain their original assets.",
                )}
            </p>
            {target ? (
                <>
                    {!detail && !error && <p role="status">{t("正在读取…", "Loading…")}</p>}
                    {detail &&
                        ("direction" in detail ? (
                            <>
                                <h3>
                                    {detail.direction === "receipt"
                                        ? t("收款详情", "Receipt details")
                                        : t("付款详情", "Payout details")}
                                </h3>
                                <dl className="confirmation-facts">
                                    <dt>{t("交易日", "Transaction date")}</dt>
                                    <dd>{detail.transaction_date}</dd>
                                    <dt>
                                        {t("往来单位（当前名称）", "Business party (current name)")}
                                    </dt>
                                    <dd>{partyName ?? detail.party_id}</dd>
                                    <dt>{t("资金账户", "Money account")}</dt>
                                    <dd>{account(detail.account_id)}</dd>
                                    <dt>{t("本金", "Principal")}</dt>
                                    <dd>{amount(detail.amount, detail.asset_id)}</dd>
                                    <dt>{t("已分摊", "Applied")}</dt>
                                    <dd>{amount(detail.applied_amount, detail.asset_id)}</dd>
                                    <dt>{t("未分配", "Unapplied")}</dt>
                                    <dd>{amount(detail.unapplied_amount, detail.asset_id)}</dd>
                                    <dt>{t("说明", "Description")}</dt>
                                    <dd>{detail.description || "—"}</dd>
                                </dl>
                                <h4>{t("独立手续费", "Separate fees")}</h4>
                                {detail.fees.length === 0 ? (
                                    <p>{t("无手续费。", "No fees.")}</p>
                                ) : (
                                    <ul className="master-records">
                                        {detail.fees.map((fee, index) => (
                                            <li key={index}>
                                                <strong>{amount(fee.amount, fee.asset_id)}</strong>
                                                <span>{account(fee.account_id)}</span>
                                                <span>
                                                    {t("分类", "Category")}: {fee.category_id}
                                                </span>
                                            </li>
                                        ))}
                                    </ul>
                                )}
                                <button
                                    type="button"
                                    onClick={() => {
                                        const id = detail.id;
                                        close();
                                        setMode("allocations");
                                        setPaymentFilter(id);
                                        setOffset(0);
                                    }}
                                >
                                    {t("查看此笔收付款的分摊", "View allocations for this payment")}
                                </button>
                                <details>
                                    <summary>
                                        {t("原记录标识", "Original record references")}
                                    </summary>
                                    <p>{detail.id}</p>
                                    <p>{detail.receipt.operation_id}</p>
                                    <p>{detail.receipt.journal_id}</p>
                                </details>
                            </>
                        ) : (
                            <>
                                <h3>{t("分摊详情", "Allocation details")}</h3>
                                <dl className="confirmation-facts">
                                    <dt>{t("分摊生效日", "Allocation date")}</dt>
                                    <dd>{detail.allocation_date}</dd>
                                    <dt>{t("使用收付款金额", "Payment amount applied")}</dt>
                                    <dd>{amount(detail.payment_amount, detail.asset_id)}</dd>
                                    <dt>{t("分摊方式", "Allocation origin")}</dt>
                                    <dd>
                                        {detail.is_initial
                                            ? t("收付款时分摊", "Allocated with payment")
                                            : t("追加分摊", "Allocated later")}
                                    </dd>
                                </dl>
                                <ol className="confirmation-lines">
                                    {detail.lines.map((line) => (
                                        <li key={line.id}>
                                            <strong>
                                                {line.description || t("单据明细", "Document line")}
                                            </strong>
                                            <p>
                                                {t("付款侧", "Payment side")}:{" "}
                                                {amount(line.payment_amount, detail.asset_id)}
                                            </p>
                                            <p>
                                                {t("单据侧", "Document side")}:{" "}
                                                {amount(
                                                    line.document_amount,
                                                    line.document_asset_id,
                                                )}
                                            </p>
                                            <p>
                                                {t("单据 / 行", "Document / line")}:{" "}
                                                {line.document_id} / {line.document_line_id}
                                            </p>
                                            <p>
                                                {t("项目", "Project")}: {line.project_id ?? "—"}
                                            </p>
                                            <details>
                                                <summary>
                                                    {t("原分录标识", "Original posting references")}
                                                </summary>
                                                <p>{line.operation_id}</p>
                                                <p>{line.journal_id}</p>
                                            </details>
                                        </li>
                                    ))}
                                </ol>
                                <details>
                                    <summary>
                                        {t("原记录标识", "Original record references")}
                                    </summary>
                                    <p>{detail.id}</p>
                                    <p>{detail.payment_id}</p>
                                </details>
                            </>
                        ))}
                    {detail &&
                        "direction" in detail &&
                        onAllocate &&
                        !/^0(?:\.0+)?$/u.test(detail.unapplied_amount) && (
                            <button type="button" onClick={() => onAllocate(detail, partyName)}>
                                {t("应用未分配款项", "Apply unapplied payment")}
                            </button>
                        )}
                    {error != null && <p role="alert">{businessError(error, locale)}</p>}
                    <button type="button" onClick={close}>
                        {t("返回收付列表", "Back to settlement list")}
                    </button>
                </>
            ) : (
                <>
                    <div className="form-actions">
                        {(["payments", "allocations"] as const).map((value) => (
                            <button
                                key={value}
                                type="button"
                                aria-pressed={mode === value}
                                onClick={() => {
                                    setMode(value);
                                    setPaymentFilter(undefined);
                                    setOffset(0);
                                }}
                            >
                                {value === "payments"
                                    ? t("收付款", "Payments")
                                    : t("全部分摊", "All allocations")}
                            </button>
                        ))}
                        <button type="button" onClick={() => setRefresh((value) => value + 1)}>
                            {t("刷新列表", "Refresh list")}
                        </button>
                    </div>
                    {paymentFilter && (
                        <p role="status">
                            {t(
                                "仅显示所选收付款的分摊；选择“全部分摊”清除筛选。",
                                "Showing allocations for the selected payment. Choose All allocations to clear this filter.",
                            )}
                        </p>
                    )}
                    {readError != null && <p role="alert">{businessError(readError, locale)}</p>}
                    {!rows && !readError && <p role="status">{t("正在读取…", "Loading…")}</p>}
                    {rows && (
                        <ul className="master-records">
                            {rows.map((row) => (
                                <li key={row.id}>
                                    {"direction" in row ? (
                                        <>
                                            <strong>
                                                {row.direction === "receipt"
                                                    ? t("收款", "Receipt")
                                                    : t("付款", "Payout")}{" "}
                                                · {amount(row.amount, row.asset_id)}
                                            </strong>
                                            <span>
                                                {row.transaction_date} · {account(row.account_id)}
                                            </span>
                                            <span>{row.description || "—"}</span>
                                            <span>
                                                {t("未分配", "Unapplied")}:{" "}
                                                {amount(row.unapplied_amount, row.asset_id)}
                                            </span>
                                        </>
                                    ) : (
                                        <>
                                            <strong>
                                                {t("分摊", "Allocation")} ·{" "}
                                                {amount(row.payment_amount, row.asset_id)}
                                            </strong>
                                            <span>
                                                {row.allocation_date} · {row.line_count}{" "}
                                                {t("行", "lines")}
                                            </span>
                                            <span>
                                                {row.is_initial
                                                    ? t("收付款时分摊", "Allocated with payment")
                                                    : t("追加分摊", "Allocated later")}
                                            </span>
                                        </>
                                    )}
                                    <button type="button" onClick={() => read(row.id)}>
                                        {t("查看详情", "View details")}
                                    </button>
                                </li>
                            ))}
                        </ul>
                    )}
                    {rows?.length === 0 && (
                        <p>{t("此页暂无收付或分摊。", "No settlements on this page.")}</p>
                    )}
                    <div className="form-actions">
                        <button
                            type="button"
                            disabled={!rows || offset === 0}
                            onClick={() => setOffset((value) => value - 25)}
                        >
                            {t("上一页", "Previous page")}
                        </button>
                        <span>
                            {t("页", "Page")} {offset / 25 + 1}
                        </span>
                        <button
                            type="button"
                            disabled={rows?.length !== 25 || offset >= 100000}
                            onClick={() => setOffset((value) => value + 25)}
                        >
                            {t("下一页", "Next page")}
                        </button>
                    </div>
                </>
            )}
        </section>
    );
}
