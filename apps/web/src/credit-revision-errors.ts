import { ApiError } from "./api";
import type { Locale } from "./i18n";

/** Revision failures never use the master-data advice to reconcile an unknown write by GET. */
export function creditRevisionError(error: unknown, locale: Locale): string {
    const messages: Record<string, [string, string]> = {
        period_closed: [
            "更正须开放所有原日期及替代日期所在期间；取消须开放原日期所在期间。请先带理由重开，不能只改新日期绕过原期间。",
            "Correction requires open periods for every original and replacement date; cancellation requires the original dates. Reopen with a reason; changing only the new date does not release the original period.",
        ],
        version_conflict: [
            "贷项组版本已变化。仅在本次请求已明确被拒绝时，才重新读取并审阅新的修订；结果未知时继续重试原请求。",
            "The credit group version changed. Reload and review a new revision only after this request is explicitly rejected; an unknown outcome still requires the original request.",
        ],
        credit_cancelled: [
            "该贷项组已取消，不能再次更正或取消。原版和历次回执仍保留。",
            "This credit group is cancelled and cannot be revised again. Its original and revision receipts remain available.",
        ],
        credit_revision_version: [
            "该贷项组已达到版本上限，不能继续修订。请保留原记录核对。",
            "This credit group has reached its version limit. Preserve the original record for review.",
        ],
        credit_date: [
            "替代贷项交易日不能早于原单据开票日。请明确修正日期。",
            "The replacement credit date cannot precede the original issue date. Explicitly correct the date.",
        ],
        credit_exceeds_source: [
            "替代净额或税额超过原行可用额度。请分别核对手填净税和其他有效贷项。",
            "The replacement net or tax exceeds source capacity. Check each manual component and the other effective credits.",
        ],
        credit_exceeds_open: [
            "替代含税额超过当前可用额度。必要时先明确退回分摊；本次修订不自动退款。",
            "The replacement gross exceeds available capacity. Explicitly return allocations if needed; this revision does not refund cash.",
        ],
        credit_revision_integrity: [
            "无法核对完整修订回执。请保留原请求，核对服务端记录，勿另建相似请求。",
            "The complete revision receipt could not be verified. Preserve the original request and check the server records; do not create a similar request.",
        ],
        credit_integrity: [
            "无法核对原贷项事实或来源。请保留原请求并核对服务端记录。",
            "The original credit facts or source could not be verified. Preserve the original request and check the server records.",
        ],
        entity_archived: [
            "主体已归档，暂停新的修订。原回执仍可重放；新意图需先恢复主体。",
            "The entity is archived and new revisions are paused. Original receipts remain replayable; restore the entity before a new intent.",
        ],
        account_archived: [
            "替代内容的账户已归档。旧事实可精确冲销，新替代引用须有效。",
            "A replacement account is archived. Old facts can be reversed exactly, but replacement references must be available.",
        ],
        category_archived: [
            "替代内容的分类已归档。旧事实可精确冲销，新替代引用须有效。",
            "A replacement category is archived. Old facts can be reversed exactly, but replacement references must be available.",
        ],
    };
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    if (error instanceof ApiError) {
        if (error.code && Object.hasOwn(messages, error.code))
            return messages[error.code][locale === "zh" ? 0 : 1];
        if (error.kind === "unauthorized")
            return t(
                "会话已失效。请以原用户登录后手动重试原请求。",
                "The session expired. Sign in as the original user, then manually retry the original request.",
            );
        if (error.kind === "forbidden")
            return t(
                "当前操作未获允许。请保留原意图并核对访问权限。",
                "This action is not permitted. Preserve the original intent and check access.",
            );
        if (error.status === 422)
            return t(
                "请核对完整替代成员、手填净税、日期、修订原因和金额精度；原输入已保留。",
                "Check the complete replacement membership, manual net and tax, dates, reason and precision; the original input is preserved.",
            );
    }
    return t(
        "暂时无法完成修订。结果未知时请保留此页并重试原请求，不按当前状态重建。",
        "The revision could not be completed. If its outcome is unknown, keep this page and retry the original request rather than recreating it from the current state.",
    );
}
