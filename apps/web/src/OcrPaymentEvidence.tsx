import { useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import { getPaymentEvidenceTarget, listBusinessPayments } from "./business-settlement-api";
import type {
    BusinessPaymentReceipt,
    BusinessPaymentSummary,
    PaymentEvidenceTarget,
} from "./business-settlement-api";
import type { Action } from "./ocr-entry";
import type { Account } from "./ledger-api";
import type { Locale } from "./i18n";
import { validPaymentEvidenceTarget } from "./payment-evidence-receipt";

/** The parent keys this private review by owner, draft/version and explicit destination. */
export function OcrPaymentEvidence({
    ledger,
    locale,
    accounts,
    action,
    locked,
    onSave,
    onReviewed,
    onCancel,
    onError,
}: {
    ledger: string;
    locale: Locale;
    accounts: Account[];
    action?: Extract<Action, { kind: "link_payment" }>;
    locked: boolean;
    onSave?: (action: Extract<Action, { kind: "link_payment" }>) => void;
    onReviewed: (receipt: BusinessPaymentReceipt | null) => void;
    onCancel?: () => void;
    onError: (error: unknown) => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [rows, setRows] = useState<BusinessPaymentSummary[]>([]);
    const [page, setPage] = useState(0),
        [id, setId] = useState(action?.payment_id ?? "");
    const [refresh, setRefresh] = useState(0),
        [listBusy, setListBusy] = useState(false);
    const [reading, setReading] = useState(false),
        [ack, setAck] = useState(false);
    const [target, setTarget] = useState<PaymentEvidenceTarget | null>(null);
    const callbacks = useRef({ onError, onReviewed });
    useEffect(() => {
        callbacks.current = { onError, onReviewed };
    }, [onError, onReviewed]);
    useEffect(() => {
        if (action) return;
        const abort = new AbortController();
        setListBusy(true);
        setRows([]);
        setId("");
        void listBusinessPayments(ledger, page * 25, abort.signal)
            .then((values) => {
                if (!abort.signal.aborted) {
                    setRows(values);
                    setListBusy(false);
                }
            })
            .catch((error) => {
                if (!abort.signal.aborted) {
                    callbacks.current.onError(error);
                    setListBusy(false);
                }
            });
        return () => abort.abort();
    }, [ledger, page, refresh, action]);
    useEffect(() => {
        const abort = new AbortController();
        setTarget(null);
        setAck(false);
        setReading(!!id);
        callbacks.current.onReviewed(null);
        if (id)
            void getPaymentEvidenceTarget(ledger, id, abort.signal)
                .then((value) => {
                    if (abort.signal.aborted) return;
                    if (!validPaymentEvidenceTarget(value, ledger, id))
                        throw new ApiError("server", 200, "invalid_response");
                    setTarget(value);
                    setReading(false);
                })
                .catch((error) => {
                    if (!abort.signal.aborted) {
                        callbacks.current.onError(error);
                        setReading(false);
                    }
                });
        return () => abort.abort();
    }, [ledger, id, refresh]);
    const payment =
        target?.payment.id === id && target.payment.ledger_id === ledger ? target.payment : null;
    const usable =
        !!payment &&
        target?.operation_status === "active" &&
        (!action || target.operation_version === action.expected_version);
    function review(checked: boolean) {
        setAck(checked);
        onReviewed(checked && usable && payment ? payment.receipt : null);
    }
    return (
        <section aria-label={t("已有经营收付款证据", "Existing business payment evidence")}>
            <p>
                {t(
                    "明确选择已登记的收付款，核对原本金、日期、账户、往来单位与手续费。只增加证据，不再次入账。",
                    "Explicitly select a recorded payment and review its original principal, date, account, party and fees. This only attaches evidence.",
                )}
            </p>
            {!action && (
                <>
                    <select
                        aria-label={t("已有经营收付款", "Existing business payment")}
                        value={id}
                        disabled={locked || listBusy}
                        onChange={(event) => {
                            review(false);
                            setId(event.target.value);
                        }}
                    >
                        <option value="">{t("请选择", "Choose")}</option>
                        {rows.map((row) => (
                            <option key={row.id} value={row.id}>
                                {row.transaction_date} · {row.amount} {row.asset_id} ·{" "}
                                {row.direction === "payout"
                                    ? t("付款", "Payout")
                                    : t("收款", "Receipt")}{" "}
                                · {row.id}
                            </option>
                        ))}
                    </select>
                    <div className="ocr-actions">
                        <button
                            disabled={locked || listBusy || page === 0}
                            onClick={() => {
                                review(false);
                                setId("");
                                setPage((p) => p - 1);
                            }}
                        >
                            {t("上一页收付款", "Previous payments")}
                        </button>
                        <button
                            disabled={locked || listBusy || rows.length < 25}
                            onClick={() => {
                                review(false);
                                setId("");
                                setPage((p) => p + 1);
                            }}
                        >
                            {t("下一页收付款", "Next payments")}
                        </button>
                    </div>
                </>
            )}
            {reading && (
                <p role="status">
                    {t("正在读取原付款回执。", "Reading the original payment receipt.")}
                </p>
            )}
            {payment && (
                <>
                    <dl className="ocr-payment-evidence">
                        <dt>{t("原付款 ID", "Original payment ID")}</dt>
                        <dd>{payment.id}</dd>
                        <dt>{t("交易日", "Transaction date")}</dt>
                        <dd>{payment.transaction_date}</dd>
                        <dt>{t("方向及本金", "Direction and principal")}</dt>
                        <dd>
                            {payment.direction === "payout"
                                ? t("付款", "Payout")
                                : t("收款", "Receipt")}{" "}
                            · {payment.amount} {payment.asset_id}
                        </dd>
                        <dt>{t("资金账户", "Money account")}</dt>
                        <dd>
                            {accounts.find((row) => row.id === payment.account_id)?.name ??
                                payment.account_id}{" "}
                            · {payment.account_id}
                        </dd>
                        <dt>{t("往来单位 ID", "Party ID")}</dt>
                        <dd>{payment.party_id}</dd>
                        <dt>{t("原凭证", "Original journal")}</dt>
                        <dd>{payment.receipt.journal_id}</dd>
                        <dt>{t("操作版本", "Operation version")}</dt>
                        <dd>{target?.operation_version}</dd>
                        <dt>{t("初始分摊行数", "Initial allocation rows")}</dt>
                        <dd>{payment.receipt.initial_allocation?.postings.length ?? 0}</dd>
                        <dt>{t("独立手续费", "Separate fees")}</dt>
                        <dd>
                            {payment.fees.length ? (
                                <ul>
                                    {payment.fees.map((fee, index) => (
                                        <li key={index}>
                                            {fee.amount} {fee.asset_id} · {fee.account_id} ·{" "}
                                            {fee.category_id}
                                        </li>
                                    ))}
                                </ul>
                            ) : (
                                t("无", "None")
                            )}
                        </dd>
                    </dl>
                    {payment.receipt.initial_allocation && (
                        <details>
                            <summary>
                                {t(
                                    "查看完整初始分摊回执",
                                    "Inspect complete initial allocation receipt",
                                )}
                            </summary>
                            <p>{payment.receipt.initial_allocation.id}</p>
                            <ol className="ocr-list">
                                {payment.receipt.initial_allocation.postings.map((row) => (
                                    <li key={row.line_no}>
                                        {row.line_no} · {row.document_id} · {row.document_line_id} ·{" "}
                                        {row.operation_id} · {row.journal_id}
                                    </li>
                                ))}
                            </ol>
                        </details>
                    )}
                    {!usable && (
                        <p role="alert">
                            {t(
                                "付款已取消或版本已变化，请重新选择并保存关联草稿。",
                                "The payment was cancelled or its version changed. Select it again and save a new link draft.",
                            )}
                        </p>
                    )}
                    <label>
                        <input
                            type="checkbox"
                            checked={ack}
                            disabled={locked || reading || !usable}
                            onChange={(event) => review(event.target.checked)}
                        />
                        {t(
                            "已核对原付款及全部初始分摊，此原件行属于该付款",
                            "I reviewed the original payment and all initial allocations; this source row belongs to it",
                        )}
                    </label>
                </>
            )}
            <div className="ocr-actions">
                <button
                    disabled={locked || reading || listBusy}
                    onClick={() => {
                        review(false);
                        setRefresh((n) => n + 1);
                    }}
                >
                    {t("刷新收付款", "Refresh payments")}
                </button>
                {onSave && (
                    <button
                        disabled={locked || reading || !usable || !ack}
                        onClick={() =>
                            target &&
                            payment &&
                            onSave({
                                kind: "link_payment",
                                payment_id: payment.id,
                                expected_version: target.operation_version,
                            })
                        }
                    >
                        {t("保存付款关联草稿", "Save payment link draft")}
                    </button>
                )}
                {onCancel && (
                    <button disabled={locked} onClick={onCancel}>
                        {t("取消关联选择", "Cancel link selection")}
                    </button>
                )}
            </div>
        </section>
    );
}
