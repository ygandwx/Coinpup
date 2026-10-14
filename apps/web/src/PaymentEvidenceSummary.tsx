import type { PaymentEvidenceTarget } from "./business-settlement-api";
import type { Account } from "./ledger-api";
import type { Locale } from "./i18n";
export function PaymentEvidenceSummary({
    target,
    accounts = [],
    locale,
}: {
    target: PaymentEvidenceTarget;
    accounts?: Account[];
    locale: Locale;
}) {
    const payment = target.payment;
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    return (
        <>
            <dl className="ocr-payment-evidence">
                <dt>{t("原付款 ID", "Original payment ID")}</dt>
                <dd>{payment.id}</dd>
                <dt>{t("交易日", "Transaction date")}</dt>
                <dd>{payment.transaction_date}</dd>
                <dt>{t("方向及本金", "Direction and principal")}</dt>
                <dd>
                    {payment.direction === "payout" ? t("付款", "Payout") : t("收款", "Receipt")} ·{" "}
                    {payment.amount} {payment.asset_id}
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
                        {t("查看完整初始分摊回执", "Inspect complete initial allocation receipt")}
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
        </>
    );
}
