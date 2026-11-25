import type { FormEvent } from "react";
import type { Locale } from "./i18n";
import type { EmailDecision, EmailDelivery } from "./reminder-email-api";

export function ReminderEmailDecision({
    row,
    locale,
    disabled,
    decide,
}: {
    row: EmailDelivery;
    locale: Locale;
    disabled: boolean;
    decide: (body: EmailDecision) => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    if (row.state === "failed") {
        return (
            <button
                disabled={disabled}
                onClick={() => decide({ action: "retry", expected_version: row.version })}
            >
                {t("计划重试邮件", "Schedule email retry")}
            </button>
        );
    }
    if (row.state !== "unknown") return null;

    function submit(event: FormEvent<HTMLFormElement>) {
        event.preventDefault();
        if (disabled) return;
        const form = new FormData(event.currentTarget);
        const action = form.get("action");
        const reason = String(form.get("reason") ?? "").trim();
        if (!reason || (action !== "verify_received" && action !== "stop")) return;
        decide({ action, reason, expected_version: row.version });
    }

    return (
        <form onSubmit={submit}>
            <fieldset className="reminder-email-fields" disabled={disabled}>
                <legend>
                    {t("记录未知投递的处理结果", "Record an outcome for the unknown delivery")}
                </legend>
                <div className="form-grid">
                    <label className="field">
                        <span>{t("处理方式", "Outcome decision")}</span>
                        <select
                            name="action"
                            defaultValue=""
                            required
                            aria-label={t("处理方式", "Outcome decision")}
                        >
                            <option value="" disabled>
                                {t("选择处理方式", "Choose an outcome")}
                            </option>
                            <option value="verify_received">
                                {t("已核实收到", "Receipt verified")}
                            </option>
                            <option value="stop">{t("停止本次投递", "Stop this delivery")}</option>
                        </select>
                    </label>
                    <label className="field">
                        <span>{t("处理理由", "Reason for decision")}</span>
                        <input name="reason" required maxLength={2000} />
                    </label>
                </div>
                <button type="submit">{t("保存处理结果", "Save outcome decision")}</button>
            </fieldset>
        </form>
    );
}
