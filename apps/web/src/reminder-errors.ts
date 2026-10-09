import { ApiError } from "./api";
import type { Locale } from "./i18n";

export function reminderError(error: unknown, locale: Locale): string {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const messages: Record<string, [string, string]> = {
        reminder_title: [
            "请填写不含控制字符的标题，最多160字。",
            "Enter a title of up to 160 characters without control characters.",
        ],
        reminder_notes: [
            "说明最多2000字，不能包含空字符。",
            "Notes allow up to 2000 characters, without NUL characters.",
        ],
        reminder_date: ["请输入有效的完整日期。", "Enter a valid complete calendar date."],
        reminder_filing_year: [
            "申报年度须为1至9999的整数。",
            "Filing year must be an integer from 1 to 9999.",
        ],
        reminder_manual_reason: [
            "请为本次人工日期变更填写新理由，最多500字。",
            "Enter a new reason for this manual date action, up to 500 characters.",
        ],
        reminder_rule_required: [
            "重算前请明确选择规则版本。",
            "Choose a rule version before recalculating.",
        ],
        reminder_rule_version_unknown: [
            "该规则版本不可用，请重新载入并明确选择。",
            "This rule version is unavailable. Reload and choose explicitly.",
        ],
        reminder_rule_kind: [
            "所选规则与事项类型不匹配。",
            "The selected rule does not match the event kind.",
        ],
        version_conflict: [
            "事项版本已变化，请核对当前记录和历史后再决定。",
            "The event version changed. Review the current record and history before deciding.",
        ],
    };
    if (error instanceof ApiError) {
        const message = messages[error.code ?? ""];
        if (message) return message[locale === "zh" ? 0 : 1];
        if (error.kind === "network")
            return t(
                "连接中断，请使用当前页面提供的恢复操作。",
                "Connection interrupted. Use the recovery action offered on this page.",
            );
        if (error.status === 422)
            return t(
                "请检查日期、必填资料和选择的规则。",
                "Check dates, required details and the selected rule.",
            );
        if (error.status === 404)
            return t("找不到事项，请重新载入。", "The event was not found. Reload to check.");
        if (error.status === 409)
            return t(
                "事项状态已变化，请核对后再操作。",
                "The event state changed. Review it before acting.",
            );
        if (error.status === 403)
            return t(
                "当前操作未获允许，请重新登录后核对。",
                "This action is not permitted. Sign in again and review.",
            );
    }
    return t(
        "暂时无法完成操作，请按页面提示重试。",
        "This action could not be completed. Retry as directed on this page.",
    );
}
