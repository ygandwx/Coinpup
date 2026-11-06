import { ApiError } from "./api";
import { businessError } from "./business-errors";
import type { Locale } from "./i18n";

export function creditError(error: unknown, locale: Locale): string {
    const messages: Record<string, [string, string]> = {
        credit_date: [
            "贷项交易日不能早于原单据开票日。原输入已保留，请明确修正日期。",
            "The credit transaction date cannot precede the original issue date. Your input is preserved; explicitly correct the date.",
        ],
        credit_exceeds_source: [
            "手填净额或税额超过原行剩余额度。请重新读取来源并分别核对，不自动按税率重算。",
            "Manual net or tax exceeds the remaining source amount. Reload and check each component; no tax rate is inferred.",
        ],
        credit_exceeds_open: [
            "可贷含税额度不足。若原款项已分摊，请先明确退回分摊；本次未自动退款。",
            "There is insufficient gross credit capacity. Explicitly return settled allocations first; this did not refund cash.",
        ],
        credit_integrity: [
            "无法核对原贷项或来源，请保留原意图并检查服务端记录。",
            "The credit or its original source could not be verified. Preserve the original intent and check the server records.",
        ],
        period_closed: [
            "贷项交易日或归属日位于已结账期间。按原日期登记须先带理由重开；日期填错时请明确修改。",
            "The credit transaction or recognition date is closed. Reopen with a reason to retain it, or explicitly correct an incorrect date.",
        ],
    };
    const code = error instanceof ApiError ? error.code : null;
    return code && Object.hasOwn(messages, code)
        ? messages[code][locale === "zh" ? 0 : 1]
        : businessError(error, locale);
}
