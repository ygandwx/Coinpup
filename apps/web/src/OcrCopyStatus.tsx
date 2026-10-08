import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { Session } from "./api";
import type { Locale } from "./i18n";
import type { OcrCopyController } from "./ocr-copy";
import { ocrError } from "./ocr-errors";

export function OcrCopyStatus({
    copies,
    session,
    locale,
    onUnauthorized,
    onResume,
    canResume,
}: {
    copies: OcrCopyController;
    session: Session;
    locale: Locale;
    onUnauthorized: () => void;
    onResume: () => void;
    canResume: boolean;
}) {
    const state = useSyncExternalStore(copies.subscribe, copies.getSnapshot);
    const unauthorized = useRef(onUnauthorized);
    const [error, setError] = useState<unknown>(null);
    useEffect(() => {
        unauthorized.current = onUnauthorized;
    }, [onUnauthorized]);
    useEffect(() => {
        if (state.status === "auth-required" || state.error?.kind === "unauthorized")
            unauthorized.current();
    }, [state.status, state.error]);
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    if (!state.context) return null;
    const labels = {
        idle: t("等待复制。", "Copy pending."),
        reading: t("正在读取原件。", "Reading original."),
        "read-failed": t("读取未完成，可重试。", "Reading did not finish. Retry is available."),
        reserving: t("正在预约目标上传。", "Reserving destination upload."),
        uploading: t("正在复制原件。", "Copying original."),
        unknown: t(
            "复制结果未知，请重试原上传。",
            "Copy outcome unknown. Retry the original upload.",
        ),
        "auth-required": t("请重新登录恢复复制。", "Sign in to resume copying."),
        rejected: t("复制被拒绝，未入账。", "Copy rejected. No entry was posted."),
        confirmed: t(
            "原件复制完成；返回源草稿明确采用后，再填写入账信息。",
            "Original copied. Return to the source draft and explicitly apply it before preparing an entry.",
        ),
        "upload-conflict": t(
            "原上传冲突，请保留页面并核对。",
            "Original upload conflict. Keep this page open and review.",
        ),
    };
    return (
        <section className="ocr-panel" aria-label={t("原件复制状态", "Original copy status")}>
            <p role="status">{labels[state.status]}</p>
            {state.error && <p role="alert">{ocrError(state.error, locale)}</p>}
            {error !== null && <p role="alert">{ocrError(error, locale)}</p>}
            {["unknown", "read-failed"].includes(state.status) && (
                <button
                    onClick={() => {
                        setError(null);
                        void copies.retry(session).catch(setError);
                    }}
                >
                    {t("重试原件复制", "Retry original copy")}
                </button>
            )}
            <button disabled={!canResume} onClick={onResume}>
                {t("返回源草稿", "Return to source draft")}
            </button>
            {["confirmed", "rejected", "read-failed"].includes(state.status) && (
                <button onClick={() => copies.dismiss()}>
                    {t(
                        "关闭复制状态（已上传原件仍保留）",
                        "Dismiss copy status (uploaded original is retained)",
                    )}
                </button>
            )}
        </section>
    );
}
