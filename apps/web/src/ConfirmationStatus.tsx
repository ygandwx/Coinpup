import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { ApiError } from "./api";
import { ocrError } from "./ocr-errors";
import type { Session } from "./api";
import type { Locale } from "./i18n";
import type { PendingConfirmationController } from "./pending-confirmations";

export function ConfirmationStatus({
    controller,
    session,
    locale,
    onUnauthorized,
}: {
    controller: PendingConfirmationController;
    session: Session;
    locale: Locale;
    onUnauthorized: () => void;
}) {
    const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
    const unauthorized = useRef(onUnauthorized);
    const [error, setError] = useState<unknown>(null);
    async function retry() {
        setError(null);
        try {
            await controller.retry(session);
        } catch (problem) {
            if (problem instanceof ApiError && problem.kind === "unauthorized")
                unauthorized.current();
            else setError(problem);
        }
    }
    useEffect(() => {
        unauthorized.current = onUnauthorized;
    }, [onUnauthorized]);
    useEffect(() => {
        if (state.status === "auth-required") unauthorized.current();
    }, [state.status]);
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    if (state.status === "idle") return null;
    const labels = {
        submitting: t("正在确认，请勿关闭页面。", "Confirming; keep this page open."),
        unknown: t(
            "结果未知。请重试原请求，勿重新记账。",
            "Outcome unknown. Retry the original request; do not create another entry.",
        ),
        "auth-required": t(
            "请重新登录后恢复原请求。",
            "Sign in again to resume the original request.",
        ),
        rejected: t(
            "请求已被拒绝，未确认的草稿可以继续修改。",
            "Request rejected. Unconfirmed drafts can be edited.",
        ),
        confirmed: t("票据确认已完成。", "Document confirmation completed."),
        "idempotency-conflict": t(
            "原意图冲突，请保留当前页面并核对记录。",
            "Original intent conflict. Keep this page open and check the records.",
        ),
    };
    return (
        <section
            className="ocr-panel"
            aria-label={t("票据确认状态", "Document confirmation status")}
        >
            <p role="status">
                {labels[state.status]} {state.index} / {state.plans.length}
            </p>
            {(state.error || error !== null) && (
                <p role="alert">{ocrError(state.error ?? error, locale)}</p>
            )}
            <ul>
                {state.receipts.map((receipt) => (
                    <li key={receipt.intent_id}>
                        {t("已确认记录", "Confirmed record")}: {receipt.operation.id}
                    </li>
                ))}
            </ul>
            {state.status === "unknown" && (
                <button onClick={() => void retry()}>
                    {t("重试原确认请求", "Retry original confirmation")}
                </button>
            )}
            {["rejected", "confirmed"].includes(state.status) && (
                <button onClick={() => controller.dismiss()}>
                    {t("关闭确认状态", "Dismiss confirmation status")}
                </button>
            )}
        </section>
    );
}
