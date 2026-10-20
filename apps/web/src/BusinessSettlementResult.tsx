import type { BusinessSettlementSnapshot } from "./pending-business-settlement";
import type { BusinessAllocationCreate, BusinessPaymentCreate } from "./business-settlement-api";
import type { AllocationPayment, AllocationSource } from "./settlement-fields";
import type { Locale } from "./i18n";
import { settlementError } from "./settlement-errors";
import { formatAmount } from "./money";
import { BusinessAllocationReturnResult } from "./BusinessAllocationReturnResult";

export type SettlementReviewData = {
    payment: AllocationPayment;
    partyName: string;
    accountName: string;
    feeLabels: { accountName: string; categoryName: string }[];
    sources: readonly AllocationSource[];
};
export function BusinessSettlementResult({
    pending,
    locale,
    onRetry,
    onEdit,
    onClose,
}: {
    pending: BusinessSettlementSnapshot;
    locale: Locale;
    onRetry: () => void;
    onEdit: () => void;
    onClose: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const plan = pending.plan;
    if (!plan || plan.kind === "refund") return null; // Refund review has its own surface.
    if (plan.kind === "return")
        return (
            <BusinessAllocationReturnResult
                pending={pending}
                locale={locale}
                onRetry={onRetry}
                onEdit={onEdit}
                onClose={onClose}
            />
        );
    const review = JSON.parse(plan.reviewedJson) as SettlementReviewData;
    const body = JSON.parse(plan.bodyJson) as BusinessPaymentCreate | BusinessAllocationCreate;
    const payment = "direction" in body ? body : null;
    const group = "direction" in body ? body.initial_allocation : body;
    const amount = (value: string, asset: string) => `${formatAmount(value, locale)} ${asset}`;
    return (
        <section aria-label={t("原提交内容", "Original submission")}>
            <h3>{t("原提交内容", "Original submission")}</h3>
            <dl className="confirmation-facts">
                <dt>{t("往来单位", "Business party")}</dt>
                <dd>{review.partyName}</dd>
                {payment && (
                    <>
                        <dt>{t("收付方向", "Payment direction")}</dt>
                        <dd>
                            {payment.direction === "receipt"
                                ? t("客户收款", "Customer receipt")
                                : t("供应商付款", "Supplier payout")}
                        </dd>
                        <dt>{t("交易日", "Transaction date")}</dt>
                        <dd>{payment.transaction_date}</dd>
                        <dt>{t("资金账户", "Money account")}</dt>
                        <dd>{review.accountName}</dd>
                        <dt>{t("本金", "Principal")}</dt>
                        <dd>{amount(payment.amount, payment.asset_id)}</dd>
                        <dt>{t("说明", "Description")}</dt>
                        <dd>{payment.description || "—"}</dd>
                    </>
                )}
                {"payment_id" in body && (
                    <>
                        <dt>{t("原收付款ID", "Original payment ID")}</dt>
                        <dd>{body.payment_id}</dd>
                    </>
                )}
                {group && (
                    <>
                        <dt>{t("分摊生效日", "Allocation date")}</dt>
                        <dd>{group.allocation_date}</dd>
                    </>
                )}
            </dl>
            {payment && (
                <ul className="master-records">
                    {(payment.fees ?? []).map((fee, index) => (
                        <li key={index}>
                            <strong>
                                {t("手续费", "Fee")} {index + 1}
                            </strong>
                            <span>{amount(fee.amount, fee.asset_id)}</span>
                            <span>
                                {review.feeLabels[index]?.accountName ?? fee.account_id} ·{" "}
                                {review.feeLabels[index]?.categoryName ?? fee.category_id}
                            </span>
                        </li>
                    ))}
                </ul>
            )}
            {group && (
                <ol className="confirmation-lines">
                    {group.lines.map((line, index) => (
                        <li key={line.document_line_id}>
                            <strong>
                                {review.sources[index]?.description ?? line.document_line_id}
                            </strong>
                            <p>
                                {t("付款侧", "Payment side")}:{" "}
                                {amount(line.payment_amount, review.payment.asset_id)}
                            </p>
                            <p>
                                {t("单据侧", "Document side")}:{" "}
                                {amount(
                                    line.document_amount,
                                    review.sources[index]?.asset_id ?? "",
                                )}
                            </p>
                        </li>
                    ))}
                </ol>
            )}
            {pending.status === "submitting" && (
                <p role="status">
                    {t("正在提交，请勿重复登记。", "Submitting. Do not record this payment again.")}
                </p>
            )}
            {pending.status === "unknown" && (
                <p role="alert">
                    {t(
                        "提交结果尚未确认。请重试原请求，不要另建收付款或分摊。",
                        "The outcome is unknown. Retry the original request instead of creating another payment or allocation.",
                    )}
                </p>
            )}
            {pending.status === "idempotency-conflict" && (
                <p role="alert">
                    {t(
                        "原幂等键与服务端记录不符。请保留此页面并核对记录，不能自动新建或清除此请求。",
                        "The original key conflicts with the saved record. Keep this page and investigate; this request cannot be automatically replaced or cleared.",
                    )}
                </p>
            )}
            {pending.status === "rejected" && (
                <p role="alert">{settlementError(pending.error, locale)}</p>
            )}
            {pending.status === "confirmed" && (
                <p role="status">
                    {t(
                        "已保存，完整原回执已核对。",
                        "Saved; the complete original receipt is verified.",
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
                        {t("返回收付列表", "Back to settlement list")}
                    </button>
                )}
            </div>
        </section>
    );
}
