import { useRef, useState } from "react";
import type { FormEvent } from "react";
import { ApiError, changePassword, getSession } from "./api";
import type { Session } from "./api";
import type { Locale } from "./i18n";

type Outcome = "mismatch" | "incorrect" | "same" | "limited" | "rejected" | "unknown" | "changed";

export function PasswordSettings({
    locale,
    session,
    onUnauthorized,
    onSessionChanged,
}: {
    locale: Locale;
    session: Session;
    onUnauthorized: () => void;
    onSessionChanged: (previous: Session, replacement: Session) => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const busyRef = useRef(false);
    const [busy, setBusy] = useState(false);
    const [outcome, setOutcome] = useState<Outcome | null>(null);
    const messages: Record<Outcome, string> = {
        mismatch: t("两次输入的新密码不一致。", "The new passwords do not match."),
        incorrect: t("当前密码不正确。", "The current password is incorrect."),
        same: t("新密码必须与当前密码不同。", "Choose a password different from the current one."),
        limited: t("尝试次数过多，请稍后再试。", "Too many attempts. Please try again later."),
        rejected: t(
            "无法修改密码，请检查输入后重试。",
            "Could not change the password. Check your inputs and try again.",
        ),
        unknown: t(
            "未能确认改密结果。请在无痕窗口中验证新密码，勿直接重复提交。",
            "The result is unknown. Verify the new password in a private window instead of submitting again.",
        ),
        changed: t(
            "密码已修改，其他浏览器和设备需要重新登录。",
            "Password changed. Sign in again on other browsers and devices.",
        ),
    };

    async function submit(event: FormEvent<HTMLFormElement>) {
        event.preventDefault();
        if (busyRef.current) return;
        setOutcome(null);
        const form = event.currentTarget;
        const data = new FormData(form);
        const current_password = String(data.get("current_password") ?? "");
        const new_password = String(data.get("new_password") ?? "");
        const confirm_password = String(data.get("confirm_password") ?? "");
        if (new_password !== confirm_password) {
            setOutcome("mismatch");
            return;
        }
        busyRef.current = true;
        setBusy(true);
        form.reset();
        try {
            const replacement = await changePassword(session.csrf_token, {
                current_password,
                new_password,
                confirm_password,
            });
            if (
                replacement.user.id !== session.user.id ||
                !/^[0-9a-f]{64}$/u.test(replacement.csrf_token) ||
                replacement.csrf_token === session.csrf_token
            ) {
                throw new ApiError("server", null, "invalid_response");
            }
            onSessionChanged(session, replacement);
            setOutcome("changed");
        } catch (error) {
            if (error instanceof ApiError) {
                if (error.kind === "unauthorized") {
                    onUnauthorized();
                    return;
                }
                if (error.code === "incorrect_current_password") {
                    setOutcome("incorrect");
                    return;
                }
                if (error.code === "new_password_must_differ") {
                    setOutcome("same");
                    return;
                }
                if (error.code === "password_confirmation_mismatch") {
                    setOutcome("mismatch");
                    return;
                }
                if (error.kind === "rate-limited") {
                    setOutcome("limited");
                    return;
                }
                if (error.status !== null && error.status >= 400 && error.status < 500) {
                    setOutcome("rejected");
                    return;
                }
            }
            // A lost response does not prove success. Refresh only the in-memory
            // session in case the browser has already accepted a replacement cookie.
            setOutcome("unknown");
            try {
                const recovered = await getSession();
                if (
                    recovered.user.id === session.user.id &&
                    /^[0-9a-f]{64}$/u.test(recovered.csrf_token)
                )
                    onSessionChanged(session, recovered);
            } catch {
                // Preserve the uncertainty and let the user verify in another window.
            }
        } finally {
            busyRef.current = false;
            setBusy(false);
        }
    }

    return (
        <section className="business-panel">
            <div className="section-heading">
                <h2>{t("修改密码", "Change password")}</h2>
            </div>
            <form aria-label={t("修改密码", "Change password")} onSubmit={(e) => void submit(e)}>
                <fieldset disabled={busy} className="password-fields">
                    <label>
                        {t("当前密码", "Current password")}
                        <input
                            name="current_password"
                            type="password"
                            autoComplete="current-password"
                            maxLength={128}
                            required
                        />
                    </label>
                    <label>
                        {t("新密码", "New password")}
                        <input
                            name="new_password"
                            type="password"
                            autoComplete="new-password"
                            minLength={12}
                            maxLength={128}
                            required
                        />
                    </label>
                    <label>
                        {t("确认新密码", "Confirm new password")}
                        <input
                            name="confirm_password"
                            type="password"
                            autoComplete="new-password"
                            minLength={12}
                            maxLength={128}
                            required
                        />
                    </label>
                </fieldset>
                <p className="help-text">
                    {t("新密码须为 12–128 个字符。", "Use 12–128 characters for the new password.")}
                </p>
                <div className="form-actions">
                    <button className="primary-button" disabled={busy}>
                        {busy ? t("保存中…", "Saving…") : t("保存新密码", "Save new password")}
                    </button>
                </div>
                {outcome && (
                    <p
                        className={outcome === "changed" ? "help-text" : "inline-error"}
                        role={outcome === "changed" ? "status" : "alert"}
                    >
                        {messages[outcome]}
                    </p>
                )}
            </form>
        </section>
    );
}
