import type { Locale } from "./i18n";
import type { FrozenReminder } from "./reminder-intent";
import { actionLabel, parameterLabel, ruleLabel } from "./reminder-labels";

export function ReminderOriginal({ plan, locale }: { plan: FrozenReminder; locale: Locale }) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const body = JSON.parse(plan.bodyJson);
    const action =
        plan.action === "transition"
            ? body.action
            : plan.action === "save"
              ? "edit"
              : plan.action === "manual"
                ? body.manual_due_date === null
                    ? "clear_manual"
                    : "set_manual"
                : plan.action;
    const fields: [string, unknown][] = [
        [t("标题", "Title"), body.title],
        [t("说明", "Notes"), body.notes],
        [t("人工日期", "Manual date"), body.manual_due_date],
        [t("操作理由", "Reason"), body.reason ?? body.manual_reason],
    ];
    return (
        <section
            data-testid="reminder-original-intent"
            aria-label={t("原事项请求", "Original event request")}
        >
            <h3>{t("原请求已保留", "Original request retained")}</h3>
            <p>
                {actionLabel(action, locale)}
                {body.expected_version
                    ? ` · ${t("原版本", "Original version")} ${body.expected_version}`
                    : ""}
            </p>
            <dl>
                {fields
                    .filter(([, value]) => value != null)
                    .map(([label, value]) => (
                        <div key={label}>
                            <dt>{label}</dt>
                            <dd>{String(value)}</dd>
                        </div>
                    ))}
            </dl>
            {body.rule && (
                <>
                    <p>
                        {ruleLabel(body.rule.rule_id, locale)} · {body.rule.rule_version}
                    </p>
                    <dl>
                        {["filing_year", "period_end", "expiry_date"]
                            .filter((key) => body.rule[key] != null)
                            .map((key) => (
                                <div key={key}>
                                    <dt>{parameterLabel(key, locale)}</dt>
                                    <dd>{String(body.rule[key])}</dd>
                                </div>
                            ))}
                    </dl>
                    <p>
                        {parameterLabel("applicability_confirmed", locale)}:{" "}
                        {body.rule.applicability_confirmed === true
                            ? t("已确认适用", "Confirmed applicable")
                            : body.rule.applicability_confirmed === false
                              ? t("已确认不适用", "Confirmed not applicable")
                              : t("尚待确认", "Awaiting confirmation")}
                    </p>
                </>
            )}
        </section>
    );
}
