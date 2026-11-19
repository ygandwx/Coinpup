import { ApiError } from "./api";
import type { BusinessConfirmationSnapshot } from "./pending-business-confirmation";
import { isDocumentRevisionPlan } from "./document-revision-intent";
import { documentRevisionDisplay } from "./document-revision-display";
import { BusinessDocumentCurrentFacts } from "./BusinessDocumentCurrentFacts";
import { BusinessDocumentRevisionFacts } from "./BusinessDocumentRevisionFacts";
import type { Locale } from "./i18n";
import "./document-revision-form.css";

export function documentRevisionError(error: unknown, locale: Locale): string {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    if (error instanceof ApiError) {
        if (error.code === "period_closed")
            return t(
                "原日期和替代日期所在期间须开放。请先带理由重开；仅改新日期不能绕过原期间。",
                "Original and replacement dates require open periods. Reopen with a reason; changing the new date does not release the original period.",
            );
        if (error.code === "document_revision_dependency")
            return t(
                "有效分摊或贷项仍依赖这些来源。必要时先显式退回分摊或修订贷项，再重新审阅单据。此操作不会自动解绑或退款。",
                "Effective allocations or credits still depend on these sources. Explicitly return allocations or revise credits when needed, then review the document again. This action does not detach evidence or refund cash automatically.",
            );
        if (["version_conflict", "document_reference_review_required"].includes(error.code ?? ""))
            return t(
                "单据或资料版本需要重新审阅。仅在本次请求明确被拒绝后重新读取；结果未知时仍须重试原请求。",
                "The document or reference version needs review. Reload only after an explicit rejection; an unknown outcome still requires the original request.",
            );
        if (error.code === "document_cancelled")
            return t(
                "此单据已取消，不能再次修订。原版与全部历史仍保留。",
                "This document is cancelled and cannot be revised again. Its original version and all history remain available.",
            );
        if (error.code === "document_revision_version")
            return t(
                "此单据已达到版本上限，请保留原记录核对。",
                "This document has reached its version limit. Preserve the original record for review.",
            );
        if (error.code === "document_revision_integrity" || error.code === "invalid_response")
            return t(
                "无法核对完整事实或回执。请保留此页及原请求，核对服务端记录，不另建相似请求。",
                "The complete facts or receipt could not be verified. Preserve this page and the original request while checking server records; do not create a similar request.",
            );
        if (error.status === 422)
            return t(
                "请核对完整行、精确金额、日期、资料版本与修订原因。原输入已保留。",
                "Check all lines, exact amounts, dates, reference versions and revision reason. The original input is preserved.",
            );
    }
    return t(
        "暂时无法完成修订。结果未知时保留此页并手动重试原请求。",
        "The revision could not be completed. If the outcome is unknown, keep this page and manually retry the original request.",
    );
}

export function BusinessDocumentRevisionResult({
    pending,
    locale,
    onRetry,
    onEdit,
    onClose,
}: {
    pending: BusinessConfirmationSnapshot;
    locale: Locale;
    onRetry: () => void;
    onEdit: () => void;
    onClose: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    if (pending.status === "auth-required")
        return (
            <p role="alert">
                {t(
                    "请以原用户登录后手动恢复本次单据修订。",
                    "Sign in as the original user to manually resume this document revision.",
                )}
            </p>
        );
    if (pending.status === "idle") return null;
    if (!isDocumentRevisionPlan(pending.plan)) return null;
    const plan = pending.plan,
        data = documentRevisionDisplay(plan, pending.status, pending.revisionReceipt);
    if (!data)
        return (
            <p role="alert">
                {t(
                    "无法安全显示本次修订。请保留此页和原请求，核对记录后再处理。",
                    "This revision cannot be displayed safely. Preserve this page and the original request while checking the records.",
                )}
            </p>
        );
    const { input, review, saved, replacement } = data;
    return (
        <section
            className="document-revision-form"
            aria-label={t("本次单据修订", "This document revision")}
        >
            <h3>
                {input.action === "correct"
                    ? t("整单更正", "Whole-document correction")
                    : t("整单取消", "Whole-document cancellation")}
            </h3>
            <p>
                {t(
                    "这里显示提交时冻结的事实。本次回执只证明这次修订，单据可能已有后续版本。",
                    "These facts were frozen for this submission. The receipt proves this revision; the document may have later versions.",
                )}
            </p>
            <dl className="confirmation-facts">
                <dt>{t("单据", "Document")}</dt>
                <dd>{plan.documentId}</dd>
                <dt>{t("版本变化", "Version change")}</dt>
                <dd>
                    {input.body.expected_version} → {input.body.expected_version + 1}
                </dd>
                <dt>{t("原因", "Reason")}</dt>
                <dd>{saved?.reason ?? input.body.reason}</dd>
                {saved && (
                    <>
                        <dt>{t("修订记录", "Revision record")}</dt>
                        <dd>{saved.revision_id}</dd>
                    </>
                )}
            </dl>
            {saved ? (
                <>
                    <p role="status">
                        {t(
                            "本次修订的完整回执已核对。",
                            "The complete receipt for this revision is verified.",
                        )}
                    </p>
                    <BusinessDocumentRevisionFacts
                        value={saved.previous}
                        locale={locale}
                        title={t("固定前版事实", "Fixed previous facts")}
                    />
                    <section aria-label={t("完整冲销凭证", "Complete reversal journals")}>
                        <h4>{t("完整冲销凭证", "Complete reversal journals")}</h4>
                        <ol className="master-records">
                            {saved.reversals.map((row) => (
                                <li key={row.source_line_id}>
                                    {row.source_line_id} · {row.journal_id}
                                </li>
                            ))}
                        </ol>
                    </section>
                </>
            ) : (
                <>
                    <p>
                        {t(
                            "下方当前单据是提交前冻结的版本；预览金额不代表已经入账。",
                            "The current document below is the version frozen before submission. Preview amounts do not prove posting.",
                        )}
                    </p>
                    <BusinessDocumentCurrentFacts current={review.current} locale={locale} />
                </>
            )}
            {replacement ? (
                <BusinessDocumentRevisionFacts
                    value={replacement}
                    locale={locale}
                    title={
                        saved
                            ? t("固定替代事实", "Fixed replacement facts")
                            : t(
                                  "已冻结替代输入与金额预览",
                                  "Frozen replacement input and amount preview",
                              )
                    }
                />
            ) : (
                <p>
                    {t(
                        "本次取消没有替代单据或替代应计。",
                        "This cancellation has no replacement document or replacement accruals.",
                    )}
                </p>
            )}
            {pending.status === "submitting" && (
                <p role="status">{t("正在提交原请求…", "Submitting the original request…")}</p>
            )}
            {pending.status === "unknown" && (
                <p role="alert">
                    {t(
                        "提交结果未知。请保留原请求并重试；不要根据当前单据或余额创建新意图。",
                        "The submission outcome is unknown. Keep and retry the original request; do not create a new intent from the current document or balance.",
                    )}
                </p>
            )}
            {pending.status === "idempotency-conflict" && (
                <p role="alert">
                    {t(
                        "原幂等键与请求不一致，不能继续或另建相似提交。请保留此页核对服务端记录。",
                        "The original idempotency key conflicts with the request. Do not continue or create a similar submission. Keep this page and check the server records.",
                    )}
                </p>
            )}
            {pending.status === "rejected" && (
                <p role="alert">
                    {t(
                        "本次请求已明确被拒绝，可以重新读取并审阅新的意图。",
                        "This request was explicitly rejected. You may reload and review a new intent.",
                    )}
                </p>
            )}
            {pending.error && <p role="alert">{documentRevisionError(pending.error, locale)}</p>}
            <div className="form-actions">
                {pending.status === "unknown" && (
                    <button type="button" onClick={onRetry}>
                        {t("重试原请求", "Retry original request")}
                    </button>
                )}
                {pending.status === "rejected" && (
                    <button type="button" onClick={onEdit}>
                        {t("重新读取并审阅", "Reload and review")}
                    </button>
                )}
                {["confirmed", "rejected"].includes(pending.status) && (
                    <button type="button" onClick={onClose}>
                        {t("关闭", "Close")}
                    </button>
                )}
            </div>
        </section>
    );
}
