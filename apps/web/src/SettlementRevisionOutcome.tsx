import type { Locale } from "./i18n";
import type { BusinessSettlementSnapshot } from "./pending-business-settlement";
import { settlementError } from "./settlement-errors";

export function SettlementRevisionOutcome({
    pending,
    receipt,
    locale,
    onRetry,
    onClose,
}: {
    pending: BusinessSettlementSnapshot;
    receipt: Readonly<{ version: number; revision_id: string }> | null;
    locale: Locale;
    onRetry: () => void;
    onClose: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    return (
        <>
            {pending.status === "submitting" && (
                <p role="status">{t("正在提交原请求…", "Submitting the original request…")}</p>
            )}
            {pending.status === "unknown" && (
                <p role="alert">
                    {t(
                        "提交结果待确认。保留此页，用原幂等键和原正文重试。",
                        "Outcome unknown. Keep this page and retry with the original key and request body.",
                    )}
                </p>
            )}
            {pending.status === "auth-required" && (
                <p role="alert">
                    {t(
                        "请用原账户重新登录以恢复请求。",
                        "Sign in again as the same user to recover this request.",
                    )}
                </p>
            )}
            {pending.status === "idempotency-conflict" && (
                <p role="alert">
                    {t(
                        "原键与已保存意图不符，需核对记录。",
                        "The original key conflicts with a saved intent. Check the recorded history.",
                    )}
                </p>
            )}
            {pending.status === "rejected" && (
                <p role="alert">{settlementError(pending.error, locale)}</p>
            )}
            {receipt && (
                <p role="status">
                    {t("已核对固定回执，版本", "Verified saved receipt, version")} {receipt.version}{" "}
                    · {receipt.revision_id}
                </p>
            )}
            {pending.status === "unknown" && (
                <button type="button" onClick={onRetry}>
                    {t("重试原请求", "Retry original request")}
                </button>
            )}
            {["confirmed", "rejected"].includes(pending.status) && (
                <button type="button" onClick={onClose}>
                    {t("返回收付列表", "Back to settlements")}
                </button>
            )}
        </>
    );
}
