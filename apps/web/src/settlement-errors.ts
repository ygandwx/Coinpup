import { ApiError } from "./api";
import { businessError } from "./business-errors";
import type { Locale } from "./i18n";

/** Definite rejections only; unknown submissions retain the original-request recovery UI. */
export function settlementError(error: unknown, locale: Locale): string {
    const messages: Record<string, readonly [string, string]> = {
        amount_range: [
            "金额或最终账户余额超出支持范围。请核对本金和各项手续费。",
            "The amount or final account balance exceeds the supported range. Check principal and every fee.",
        ],
        allocation_date: [
            "分摊日期不得早于原付款日或任一所选单据的开票日。",
            "Allocation cannot precede the original payment or any selected document's issue date.",
        ],
        allocation_amount_mismatch: [
            "同一资产的付款侧与单据侧数量必须相等。",
            "Payment and document quantities must match when they use the same asset.",
        ],
        allocation_exceeds_document: [
            "所选单据的未结余额不足。请明确重新选择明细，核对最新余额后再分摊。",
            "A selected document has less outstanding. Reselect its lines and review current amounts before applying.",
        ],
        allocation_exceeds_payment: [
            "分摊超过这笔付款的未分配余额。请返回列表核对当前余量，再明确登记分摊。",
            "Allocation exceeds this payment's unapplied balance. Return to the list and review current capacity before applying.",
        ],
        period_closed: [
            "付款或分摊日期位于已结账期间。如需按原日期登记，请填写理由重开；若日期填错，请先明确更正。",
            "The payment or allocation date is closed. Reopen with a reason to retain it, or explicitly correct an incorrectly entered date.",
        ],
    };
    const code = error instanceof ApiError ? error.code : null;
    return code && Object.hasOwn(messages, code)
        ? messages[code][locale === "zh" ? 0 : 1]
        : businessError(error, locale);
}
