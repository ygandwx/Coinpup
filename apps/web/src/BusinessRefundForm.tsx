import { useEffect, useId, useRef, useState } from "react";
import { ApiError } from "./api";
import type { Locale } from "./i18n";
import type { Account, Asset, Category } from "./ledger-api";
import type { BusinessPaymentDetail } from "./business-settlement-api";
import { getRefundCapacity } from "./refund-api";
import type { RefundCapacity, RefundCreate } from "./refund-api";
import { listAllocationReturns } from "./allocation-return-api";
import type { AllocationReturnSummary } from "./allocation-return-api";
import { validRefundReview } from "./refund-intent";
import type { RefundReview } from "./refund-intent";
import { PaymentMoneyFields } from "./BusinessPaymentFields";
import { settlementError } from "./settlement-errors";
import { formatAmount } from "./money";
import { validRefundCapacity } from "./refund-capacity";

export type RefundEditor = {
    payment: BusinessPaymentDetail;
    body: RefundCreate;
    capacity: RefundCapacity | null;
    selectedReturns: RefundReview["returns"];
};

export function BusinessRefundForm({
    ledgerId,
    locale,
    accounts,
    assets,
    categories,
    draft,
    disabled,
    onChange,
    onSubmit,
    onCancel,
    onUnauthorized,
}: {
    ledgerId: string;
    locale: Locale;
    accounts: Account[];
    assets: Asset[];
    categories: Category[];
    draft: RefundEditor;
    disabled: boolean;
    onChange: (update: (value: RefundEditor) => RefundEditor) => void;
    onSubmit: (body: RefundCreate, review: RefundReview) => Promise<void>;
    onCancel: () => void;
    onUnauthorized: () => void;
}) {
    const prefix = useId();
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [error, setError] = useState<unknown>(null),
        [refresh, setRefresh] = useState(0);
    const [reviewed, setReviewed] = useState(false),
        [invalid, setInvalid] = useState(false);
    const [offset, setOffset] = useState(0);
    const [returns, setReturns] = useState<{ key: string; rows: AllocationReturnSummary[] } | null>(
        null,
    );
    const [returnError, setReturnError] = useState<{ key: string; error: unknown } | null>(null);
    const callbacks = useRef({ onChange, onUnauthorized });
    callbacks.current = { onChange, onUnauthorized };
    const { payment, body, capacity } = draft;
    const paymentId = payment.id;
    const key = `${ledgerId}/${paymentId}/${offset}/${refresh}`;
    const page = returns?.key === key ? returns.rows : null;
    useEffect(() => {
        if (capacity) return;
        const abort = new AbortController();
        getRefundCapacity(ledgerId, paymentId, abort.signal)
            .then((value) => {
                if (abort.signal.aborted) return;
                if (!validRefundCapacity(ledgerId, payment, value))
                    throw new ApiError("server", 200, "invalid_response");
                setError(null);
                callbacks.current.onChange((current) =>
                    current.payment.id === paymentId ? { ...current, capacity: value } : current,
                );
            })
            .catch((caught) => {
                if (abort.signal.aborted) return;
                if (caught instanceof ApiError && caught.status === 401)
                    callbacks.current.onUnauthorized();
                else setError(caught);
            });
        return () => abort.abort();
    }, [ledgerId, paymentId, payment, capacity, refresh]);
    useEffect(() => {
        const abort = new AbortController();
        listAllocationReturns(ledgerId, paymentId, offset, abort.signal)
            .then((rows) => {
                if (abort.signal.aborted) return;
                if (rows.some((row) => row.ledger_id !== ledgerId || row.payment_id !== paymentId))
                    throw new ApiError("server", 200, "invalid_response");
                setReturns({ key, rows });
                setReturnError(null);
            })
            .catch((caught) => {
                if (abort.signal.aborted) return;
                if (caught instanceof ApiError && caught.status === 401)
                    callbacks.current.onUnauthorized();
                else setReturnError({ key, error: caught });
            });
        return () => abort.abort();
    }, [ledgerId, paymentId, offset, key]);
    const update = (next: RefundCreate) => {
        setReviewed(false);
        setInvalid(false);
        onChange((current) => ({ ...current, body: next }));
    };
    const review: RefundReview | null = capacity
        ? {
              capacity,
              paymentOperationId: payment.receipt.operation_id,
              paymentJournalId: payment.receipt.journal_id,
              accounts,
              assets,
              categories,
              returns: draft.selectedReturns,
          }
        : null;
    const refs = { accounts, assets, categories, parties: [] };
    const fees = body.fees ?? [];
    const amount = (value: string, asset: string) => `${formatAmount(value, locale)} ${asset}`;
    return (
        <form
            aria-label={t("实际退款", "Actual refund")}
            onSubmit={(event) => {
                event.preventDefault();
                if (
                    disabled ||
                    !reviewed ||
                    !review ||
                    !validRefundReview(ledgerId, body, review)
                ) {
                    setInvalid(true);
                    return;
                }
                void onSubmit(body, review);
            }}
        >
            <p className="help-text">
                {t(
                    "实际退款独立记录资金流，不自动冲减费用或收入。已有分摊须先明确退回；退回与退款分别保存，退款失败不会撤销已保存的退回。",
                    "A refund records actual cash independently and does not reduce expense or income. Return existing allocations first; returns and refunds are saved separately. A rejected refund does not undo a saved return.",
                )}
            </p>
            <dl className="confirmation-facts">
                <dt>{t("原付款标识", "Original payment ID")}</dt>
                <dd>{paymentId}</dd>
                <dt>{t("退款方向", "Refund direction")}</dt>
                <dd>
                    {payment.direction === "receipt"
                        ? t("退给客户，资金减少", "Return to customer; cash decreases")
                        : t("供应商退款，资金增加", "Refund from supplier; cash increases")}
                </dd>
                <dt>{t("原本金", "Original principal")}</dt>
                <dd>{amount(payment.amount, payment.asset_id)}</dd>
                <dt>{t("当前可退本金", "Current refundable principal")}</dt>
                <dd>
                    {capacity
                        ? amount(capacity.refundable_amount, capacity.asset_id)
                        : t("读取中…", "Loading…")}
                </dd>
            </dl>
            {error != null && <p role="alert">{settlementError(error, locale)}</p>}
            <button
                type="button"
                disabled={disabled}
                onClick={() => {
                    setReviewed(false);
                    onChange((current) => ({ ...current, capacity: null }));
                    setRefresh((value) => value + 1);
                }}
            >
                {t("重新读取可退量", "Reload refundable capacity")}
            </button>
            <fieldset disabled={disabled}>
                <legend>{t("本次实际资金", "Actual cash movement")}</legend>
                <PaymentMoneyFields
                    value={body}
                    refs={refs}
                    locale={locale}
                    fee={false}
                    amountLabel={t("实际退款数量", "Actual refund quantity")}
                    onChange={(money) => update({ ...body, ...money })}
                />
                <div className="form-grid">
                    <label className="field">
                        {t("退还原付款本金", "Original principal being refunded")} (
                        {payment.asset_id})
                        <input
                            type="text"
                            inputMode="decimal"
                            required
                            maxLength={40}
                            value={body.payment_amount}
                            onChange={(event) =>
                                update({ ...body, payment_amount: event.target.value })
                            }
                        />
                    </label>
                    <label className="field">
                        {t("退款交易日", "Refund transaction date")}
                        <input
                            type="date"
                            required
                            min={payment.transaction_date}
                            value={body.transaction_date}
                            onChange={(event) =>
                                update({ ...body, transaction_date: event.target.value })
                            }
                        />
                    </label>
                </div>
                <p className="help-text">
                    {t(
                        "同资产两种数量必须相等；跨资产请分别明确填写实际数量，不自动换算。",
                        "For the same asset, both quantities must match. For different assets, enter both actual quantities explicitly; no conversion is inferred.",
                    )}
                </p>
                <div className="field">
                    <label htmlFor={`${prefix}-reason`}>{t("退款原因", "Refund reason")}</label>
                    <textarea
                        id={`${prefix}-reason`}
                        required
                        maxLength={2000}
                        value={body.reason}
                        onChange={(event) => update({ ...body, reason: event.target.value })}
                    />
                </div>
                <fieldset>
                    <legend>
                        {t(
                            "关联已保存的分摊退回（可选）",
                            "Link saved allocation returns (optional)",
                        )}
                    </legend>
                    <p>
                        {t(
                            "仅保留来源关联，不再次释放本金。",
                            "References preserve provenance without releasing principal again.",
                        )}
                    </p>
                    {returnError?.key === key && (
                        <p role="alert">{settlementError(returnError.error, locale)}</p>
                    )}
                    {!page && returnError?.key !== key && (
                        <p role="status">{t("读取中…", "Loading…")}</p>
                    )}
                    {page?.length === 0 && (
                        <p>{t("本页无退回记录。", "No returns on this page.")}</p>
                    )}
                    {page?.map((row) => (
                        <label className="archive-toggle" key={row.id}>
                            <input
                                type="checkbox"
                                checked={draft.selectedReturns.some((item) => item.id === row.id)}
                                onChange={(event) => {
                                    const checked = event.currentTarget.checked;
                                    setReviewed(false);
                                    onChange((current) => {
                                        const selectedReturns = checked
                                            ? [
                                                  ...current.selectedReturns.filter(
                                                      (item) => item.id !== row.id,
                                                  ),
                                                  row,
                                              ]
                                            : current.selectedReturns.filter(
                                                  (item) => item.id !== row.id,
                                              );
                                        return {
                                            ...current,
                                            selectedReturns,
                                            body: {
                                                ...current.body,
                                                allocation_return_ids: selectedReturns.map(
                                                    (item) => item.id,
                                                ),
                                            },
                                        };
                                    });
                                }}
                            />
                            {row.return_date} · {row.reason} · {row.id}
                        </label>
                    ))}
                    <p>
                        {t("已选来源数", "Selected references")}: {draft.selectedReturns.length}
                    </p>
                    <div className="form-actions allocation-pagination">
                        <button
                            type="button"
                            disabled={offset === 0}
                            onClick={() => setOffset((value) => value - 25)}
                        >
                            {t("上一页", "Previous page")}
                        </button>
                        <button
                            type="button"
                            disabled={page?.length !== 25}
                            onClick={() => setOffset((value) => value + 25)}
                        >
                            {t("下一页", "Next page")}
                        </button>
                        <button type="button" onClick={() => setRefresh((value) => value + 1)}>
                            {t("重读来源", "Reload references")}
                        </button>
                        <button
                            type="button"
                            onClick={() => {
                                setReviewed(false);
                                onChange((current) => ({
                                    ...current,
                                    selectedReturns: [],
                                    body: { ...current.body, allocation_return_ids: [] },
                                }));
                            }}
                        >
                            {t("清除所选来源", "Clear selected references")}
                        </button>
                    </div>
                </fieldset>
                <fieldset>
                    <legend>{t("独立手续费", "Separate fees")}</legend>
                    {fees.map((fee, index) => (
                        <fieldset key={index}>
                            <legend>
                                {t("手续费", "Fee")} {index + 1}
                            </legend>
                            <PaymentMoneyFields
                                value={fee}
                                refs={refs}
                                locale={locale}
                                fee
                                onChange={(money) =>
                                    update({
                                        ...body,
                                        fees: fees.map((item, i) =>
                                            i === index ? { ...item, ...money } : item,
                                        ),
                                    })
                                }
                            />
                            <div className="field">
                                <label htmlFor={`${prefix}-fee-category-${index}`}>
                                    {t("手续费分类", "Fee category")}
                                </label>
                                <select
                                    id={`${prefix}-fee-category-${index}`}
                                    required
                                    value={fee.category_id}
                                    onChange={(event) =>
                                        update({
                                            ...body,
                                            fees: fees.map((item, i) =>
                                                i === index
                                                    ? { ...item, category_id: event.target.value }
                                                    : item,
                                            ),
                                        })
                                    }
                                >
                                    <option value="">{t("请选择", "Choose")}</option>
                                    {categories
                                        .filter(
                                            (row) =>
                                                !row.archived &&
                                                row.kind === "expense" &&
                                                row.ledger_id === ledgerId,
                                        )
                                        .map((row) => (
                                            <option key={row.id} value={row.id}>
                                                {row.name}
                                            </option>
                                        ))}
                                </select>
                            </div>
                            <button
                                type="button"
                                onClick={() =>
                                    update({ ...body, fees: fees.filter((_, i) => i !== index) })
                                }
                            >
                                {t("移除此手续费", "Remove this fee")}
                            </button>
                        </fieldset>
                    ))}
                    <button
                        type="button"
                        disabled={fees.length >= 20}
                        onClick={() =>
                            update({
                                ...body,
                                fees: [
                                    ...fees,
                                    { account_id: "", asset_id: "", amount: "", category_id: "" },
                                ],
                            })
                        }
                    >
                        {t("添加手续费", "Add fee")}
                    </button>
                </fieldset>
                <label className="archive-toggle">
                    <input
                        type="checkbox"
                        checked={reviewed}
                        disabled={!capacity}
                        onChange={(event) => setReviewed(event.currentTarget.checked)}
                    />
                    {t(
                        "已核对原本金、实际资金、日期、独立手续费和所选退回；本次不冲减费用。",
                        "I reviewed original principal, actual cash, date, separate fees and selected returns; this refund does not reduce expense.",
                    )}
                </label>
                {invalid && (
                    <p role="alert">
                        {t(
                            "请核对可退本金、两侧精确数量、日期、有效账户/分类及所选来源，再确认。",
                            "Check refundable principal, both exact quantities, dates, active accounts/categories and selected sources before confirming.",
                        )}
                    </p>
                )}
                <div className="form-actions">
                    <button
                        className="primary-button"
                        type="submit"
                        disabled={!capacity || !reviewed}
                    >
                        {t("登记实际退款", "Record actual refund")}
                    </button>
                    <button type="button" onClick={onCancel}>
                        {t("取消", "Cancel")}
                    </button>
                </div>
            </fieldset>
        </form>
    );
}
