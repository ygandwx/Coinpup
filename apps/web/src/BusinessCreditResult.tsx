import type { BusinessCreditSnapshot } from "./pending-business-credit";
import type { CreditCreate } from "./credit-api";
import type { CreditReview } from "./credit-intent";
import type { Locale } from "./i18n";
import { formatAmount, sumAmounts } from "./money";
import { creditError } from "./credit-errors";
import "./credit-form.css";

export function BusinessCreditResult({
    pending,
    locale,
    onRetry,
    onEdit,
    onClose,
}: {
    pending: BusinessCreditSnapshot;
    locale: Locale;
    onRetry: () => void;
    onEdit: () => void;
    onClose: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    if (!pending.plan) return null;
    const body = JSON.parse(pending.plan.bodyJson) as CreditCreate;
    const review = JSON.parse(pending.plan.reviewedJson) as CreditReview;
    const amount = (value: string) => `${formatAmount(value, locale)} ${review.asset.asset_id}`;
    return (
        <section className="credit-form" aria-label={t("原贷项提交", "Original credit submission")}>
            <h3>{t("原贷项提交", "Original credit submission")}</h3>
            <p>
                {t(
                    "冲减原分类，不自动改变实际资金。",
                    "Reduces the original classification without changing actual cash.",
                )}
            </p>
            <dl className="confirmation-facts">
                <dt>{t("原单据标识", "Original document ID")}</dt>
                <dd>{body.document_id}</dd>
                <dt>{t("贷项交易日", "Credit transaction date")}</dt>
                <dd>{body.credit_date}</dd>
                <dt>{t("贷项原因", "Credit reason")}</dt>
                <dd>{body.reason}</dd>
            </dl>
            <ul className="master-records">
                {body.lines.map((line) => {
                    const source = review.capacity.lines.find(
                        (row) => row.source_line_id === line.source_line_id,
                    )!;
                    return (
                        <li key={line.source_line_id}>
                            <strong>{source.description}</strong>
                            <span>
                                {t("净额", "Net")}: {amount(line.net_amount)}
                            </span>
                            <span>
                                {t("税额", "Tax")}: {amount(line.tax_amount)}
                            </span>
                            <span>
                                {t("含税额", "Gross")}:{" "}
                                {amount(
                                    sumAmounts(
                                        [line.net_amount, line.tax_amount],
                                        review.asset.scale,
                                    ),
                                )}
                            </span>
                            <span>
                                {t("归属日", "Recognition date")}: {line.recognition_date}
                            </span>
                        </li>
                    );
                })}
            </ul>
            {pending.status === "submitting" && (
                <p role="status">{t("正在提交原意图…", "Submitting the original intent…")}</p>
            )}
            {pending.status === "unknown" && (
                <p role="alert">
                    {t(
                        "贷项结果尚未确认，请保留此页并重试原请求，不按当前余额另建贷项。刷新或关闭会丢失内存意图，请先核对已保存历史。",
                        "The credit outcome is unknown. Keep this page and retry the original request; do not create another credit from current balances. Refreshing or closing loses the in-memory intent; check saved history first.",
                    )}
                </p>
            )}
            {pending.status === "idempotency-conflict" && (
                <p role="alert">
                    {t(
                        "原幂等键与服务端记录不符，请保留此页面核对，不能自动替换请求。",
                        "The original key conflicts with the saved record. Keep this page and investigate; the request cannot be automatically replaced.",
                    )}
                </p>
            )}
            {pending.status === "rejected" && (
                <p role="alert">{creditError(pending.error, locale)}</p>
            )}
            {pending.status === "confirmed" && (
                <p role="status">
                    {t(
                        "原贷项请求已保存，完整行回执已核对。",
                        "The original credit request is saved; its complete line receipt is verified.",
                    )}
                </p>
            )}
            <div className="form-actions">
                {pending.status === "unknown" && (
                    <button type="button" onClick={onRetry}>
                        {t("重试原请求", "Retry original request")}
                    </button>
                )}
                {pending.status === "rejected" && (
                    <button type="button" onClick={onEdit}>
                        {t("修改被拒绝的请求", "Edit rejected request")}
                    </button>
                )}
                {["confirmed", "rejected"].includes(pending.status) && (
                    <button type="button" onClick={onClose}>
                        {t("返回贷项列表", "Back to credit list")}
                    </button>
                )}
            </div>
        </section>
    );
}
