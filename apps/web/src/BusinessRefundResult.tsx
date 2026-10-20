import type { BusinessSettlementSnapshot } from "./pending-business-settlement";
import type { RefundCreate } from "./refund-api";
import type { RefundReview } from "./refund-intent";
import type { Locale } from "./i18n";
import { formatAmount } from "./money";
import { settlementError } from "./settlement-errors";

export function BusinessRefundResult({
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
    if (!pending.plan || pending.plan.kind !== "refund") return null;
    const body = JSON.parse(pending.plan.bodyJson) as RefundCreate;
    const review = JSON.parse(pending.plan.reviewedJson) as RefundReview;
    const amount = (value: string, asset: string) => `${formatAmount(value, locale)} ${asset}`;
    return (
        <section aria-label={t("原提交内容", "Original submission")}>
            <h3>{t("原实际退款内容", "Original actual refund")}</h3>
            <p>
                {t(
                    "仅记录真实资金流，费用或收入不因退款自动改变。",
                    "This records actual cash only; expense or income does not change automatically.",
                )}
            </p>
            <dl className="confirmation-facts">
                <dt>{t("原付款标识", "Original payment ID")}</dt>
                <dd>{body.payment_id}</dd>
                <dt>{t("退款方向", "Refund direction")}</dt>
                <dd>
                    {review.capacity.payment_direction === "receipt"
                        ? t("退给客户，资金减少", "Return to customer; cash decreases")
                        : t("供应商退款，资金增加", "Refund from supplier; cash increases")}
                </dd>
                <dt>{t("实际退款数量", "Actual refund quantity")}</dt>
                <dd>{amount(body.amount, body.asset_id)}</dd>
                <dt>{t("退还原付款本金", "Original principal being refunded")}</dt>
                <dd>{amount(body.payment_amount, review.capacity.asset_id)}</dd>
                <dt>{t("资金账户标识", "Money account ID")}</dt>
                <dd>{body.account_id}</dd>
                <dt>{t("退款交易日", "Refund transaction date")}</dt>
                <dd>{body.transaction_date}</dd>
                <dt>{t("退款原因", "Refund reason")}</dt>
                <dd>{body.reason}</dd>
            </dl>
            <h4>{t("独立手续费", "Separate fees")}</h4>
            {!body.fees?.length ? (
                <p>{t("无手续费。", "No fees.")}</p>
            ) : (
                <ul className="master-records">
                    {body.fees.map((fee, index) => (
                        <li key={index}>
                            <strong>{amount(fee.amount, fee.asset_id)}</strong>
                            <span>
                                {t("账户标识", "Account ID")}: {fee.account_id}
                            </span>
                            <span>
                                {t("分类标识", "Category ID")}: {fee.category_id}
                            </span>
                        </li>
                    ))}
                </ul>
            )}
            <details>
                <summary>{t("所选退回来源", "Selected return references")}</summary>
                <ul>
                    {(body.allocation_return_ids ?? []).map((id) => (
                        <li key={id}>{id}</li>
                    ))}
                </ul>
            </details>
            {pending.status === "submitting" && (
                <p role="status">{t("正在提交原意图…", "Submitting the original intent…")}</p>
            )}
            {pending.status === "unknown" && (
                <p role="alert">
                    {t(
                        "退款结果尚未确认。请保留此页并重试原请求，不要根据余额另建退款。刷新或关闭会丢失内存意图，请先核对已保存的退款历史。",
                        "The refund outcome is unknown. Keep this page and retry the original request; do not create another refund from the balance. Refreshing or closing loses the in-memory intent; check saved refund history first.",
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
