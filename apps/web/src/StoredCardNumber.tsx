import { useCallback, useEffect, useRef, useState } from "react";
import { validCardNumber } from "./account-details";
import { ApiError } from "./api";
import type { Locale } from "./i18n";

const displayDuration = 30_000;

export function StoredCardNumber({
    masked,
    locale,
    onReveal,
    onUnauthorized,
}: {
    masked: string;
    locale: Locale;
    onReveal: (signal: AbortSignal) => Promise<string | null>;
    onUnauthorized: () => void;
}) {
    const [number, setNumber] = useState<string | null>(null);
    const [loading, setLoading] = useState(false);
    const [status, setStatus] = useState<"copied" | "copyError" | "readError" | null>(null);
    const pending = useRef<AbortController | null>(null);
    const expiry = useRef<number | undefined>(undefined);
    // An in-flight clipboard write from an earlier reveal cannot extend a newer one.
    const activeReveal = useRef(0);
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);

    const hide = useCallback(() => {
        activeReveal.current += 1;
        pending.current?.abort();
        pending.current = null;
        window.clearTimeout(expiry.current);
        setNumber(null);
        setLoading(false);
        setStatus(null);
    }, []);

    useEffect(() => {
        function concealOnHidden() {
            if (document.hidden) hide();
        }
        document.addEventListener("visibilitychange", concealOnHidden);
        window.addEventListener("pagehide", hide);
        return () => {
            document.removeEventListener("visibilitychange", concealOnHidden);
            window.removeEventListener("pagehide", hide);
            activeReveal.current += 1;
            pending.current?.abort();
            window.clearTimeout(expiry.current);
        };
    }, [hide]);

    function expireAfterDelay() {
        window.clearTimeout(expiry.current);
        expiry.current = window.setTimeout(hide, displayDuration);
    }

    async function reveal() {
        if (pending.current || document.hidden) return;
        const request = new AbortController();
        pending.current = request;
        setLoading(true);
        setStatus(null);
        try {
            const value = await onReveal(request.signal);
            if (request.signal.aborted) return;
            if (value && validCardNumber(value) && value.endsWith(masked.slice(-4))) {
                activeReveal.current += 1;
                setNumber(value);
                expireAfterDelay();
            } else {
                setStatus("readError");
            }
        } catch (error) {
            if (request.signal.aborted) return;
            if (error instanceof ApiError && error.kind === "unauthorized") onUnauthorized();
            else setStatus("readError");
        } finally {
            if (pending.current === request) {
                pending.current = null;
                setLoading(false);
            }
        }
    }

    async function copy() {
        if (!number) return;
        const revealId = activeReveal.current;
        try {
            await navigator.clipboard.writeText(number);
            if (revealId !== activeReveal.current) return;
            expireAfterDelay();
            setStatus("copied");
        } catch {
            if (revealId === activeReveal.current) setStatus("copyError");
        }
    }

    return (
        <div className="field stored-card-number">
            <p className="field-label">{t("已保存的卡号", "Saved card number")}</p>
            <div
                className="account-card-number"
                role="group"
                aria-label={t("已保存的卡号", "Saved card number")}
            >
                {number ?? masked}
            </div>
            <div className="card-number-actions">
                <button
                    type="button"
                    className="secondary-button"
                    aria-label={
                        number
                            ? t("隐藏卡号", "Hide card number")
                            : t("显示卡号", "Show card number")
                    }
                    disabled={loading}
                    onClick={() => (number ? hide() : void reveal())}
                >
                    {number
                        ? t("隐藏", "Hide")
                        : loading
                          ? t("读取中…", "Loading…")
                          : t("显示", "Show")}
                </button>
                <button
                    type="button"
                    className="secondary-button"
                    aria-label={t("复制卡号", "Copy card number")}
                    disabled={!number}
                    onClick={() => void copy()}
                >
                    {t("复制", "Copy")}
                </button>
            </div>
            {status === "copied" && (
                <p className="help-text" role="status">
                    {t("已复制，30 秒后隐藏。", "Copied. The display hides in 30 seconds.")}
                </p>
            )}
            {status === "copyError" && (
                <p className="inline-error" role="alert">
                    {t("无法访问剪贴板。", "The clipboard is unavailable.")}
                </p>
            )}
            {status === "readError" && (
                <p className="inline-error" role="alert">
                    {t(
                        "无法显示已保存的卡号，请核对账户。",
                        "Unable to show the saved card number. Check the account.",
                    )}
                </p>
            )}
        </div>
    );
}
