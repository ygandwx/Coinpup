import { ApiError } from "./api";
import type { Locale } from "./i18n";

export function businessError(error: unknown, locale: Locale): string {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    if (!(error instanceof ApiError))
        return t("暂时无法完成操作，请重试。", "This action could not be completed. Please retry.");
    if (error.kind === "network")
        return t(
            "连接中断，保存结果可能尚未确认。请刷新列表核对后重试。",
            "The connection was interrupted. Check the refreshed list before retrying an unconfirmed save.",
        );
    if (error.code === "version_conflict")
        return t(
            "这条记录已被修改。你的输入已保留；选择“重新载入”会替换当前输入。",
            "This record changed elsewhere. Your input is preserved; Reload replaces it with the latest record.",
        );
    if (error.code === "period_closed")
        return t(
            "交易日或归属日位于已结账期间。请先在“结账与重开”中填写理由重开。",
            "The transaction or recognition date is closed. Reopen it with a reason in Period closing first.",
        );
    if (error.code === "invalid_period_transition")
        return t(
            "结账不能后退截止日；重开必须缩小截止日或解除关闭。",
            "Closing cannot move the cutoff backward. Reopening must reduce or remove it.",
        );
    if (error.code === "opening_exists")
        return t(
            "此账户和资产已有期初记录。请在流水中核对原记录。",
            "This account and asset already have an opening entry. Check the original transaction.",
        );
    if (error.code === "operation_cancelled")
        return t(
            "此记录已取消，无法再次更正或取消。仍可查看原记录和版本历史。",
            "This entry is already cancelled and cannot be changed again. Its original content and history remain available.",
        );
    if (error.code === "duplicate_record" || error.status === 409)
        return t(
            "记录状态已变化或已经存在。请刷新核对，当前输入已保留。",
            "The record changed or already exists. Refresh to check; your input is preserved.",
        );
    if (error.status === 422)
        return t(
            "请检查必填内容、金额精度和所选账本的有效账户或分类。",
            "Check required fields, amount precision and the available accounts or categories in this ledger.",
        );
    if (error.status === 404)
        return t("找不到这条记录，请刷新列表。", "This record was not found. Refresh the list.");
    if (error.kind === "forbidden")
        return t(
            "当前操作未获允许，请重新登录后重试。",
            "This action is not permitted. Sign in again and retry.",
        );
    return t("服务暂时不可用，请稍后重试。", "The service is unavailable. Please retry later.");
}
