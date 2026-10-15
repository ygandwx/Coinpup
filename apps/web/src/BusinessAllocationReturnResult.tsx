import type { BusinessSettlementSnapshot } from "./pending-business-settlement";
import type { AllocationReturnCreate } from "./allocation-return-api";
import type { AllocationReturnReview } from "./allocation-return-intent";
import type { Locale } from "./i18n";
import { formatAmount } from "./money";
import { settlementError } from "./settlement-errors";

export function BusinessAllocationReturnResult({
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
    if (!pending.plan || pending.plan.kind !== "return") return null;
    const body = JSON.parse(pending.plan.bodyJson) as AllocationReturnCreate;
    const review = JSON.parse(pending.plan.reviewedJson) as AllocationReturnReview;
    return (
        <section aria-label={t("原提交内容", "Original submission")}>
            <h3>{t("原分摊退回内容", "Original allocation return")}</h3>
            <dl className="confirmation-facts">
                <dt>{t("退回生效日", "Return effective date")}</dt>
                <dd>{body.return_date}</dd>
                <dt>{t("退回原因", "Return reason")}</dt>
                <dd>{body.reason}</dd>
            </dl>
            <ol className="confirmation-lines">
                {body.lines.map((line) => {
                    const source = review.capacity.lines.find(
                        (row) => row.source_allocation_line_id === line.source_allocation_line_id,
                    )!;
                    return (
                        <li key={line.source_allocation_line_id}>
                            <strong>{source.description || t("单据明细", "Document line")}</strong>
                            <p>
                                {t("付款侧退回", "Payment amount returned")}:{" "}
                                {formatAmount(line.payment_amount, locale)}{" "}
                                {review.capacity.asset_id}
                            </p>
                            <p>
                                {t("单据侧退回", "Document amount returned")}:{" "}
                                {formatAmount(line.document_amount, locale)}{" "}
                                {source.document_asset_id}
                            </p>
                            <details>
                                <summary>{t("原来源标识", "Original source references")}</summary>
                                <p>{line.source_allocation_line_id}</p>
                                <p>
                                    {source.document_id} / {source.document_line_id}
                                </p>
                            </details>
                        </li>
                    );
                })}
            </ol>
            {pending.status === "submitting" && (
                <p role="status">{t("正在提交原意图…", "Submitting the original intent…")}</p>
            )}
            {pending.status === "unknown" && (
                <p role="alert">
                    {t(
                        "提交结果尚未确认。请保留此页面，只重试原请求；不要根据当前余额另建退回。刷新或关闭页面会丢失内存中的原意图，请先核对已保存记录。",
                        "The outcome is unknown. Keep this page and retry the original request; do not create another return from the current balance. Refreshing or closing loses the in-memory intent; check saved records first.",
                    )}
                </p>
            )}
            {pending.status === "idempotency-conflict" && (
                <p role="alert">
                    {t(
                        "原幂等键与服务端记录不符。请保留此页面并核对记录，不能自动替换请求。",
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
