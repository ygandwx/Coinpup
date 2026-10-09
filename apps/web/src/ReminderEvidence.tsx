import type { Locale } from "./i18n";
import { parameterLabel, reasonLabel, ruleLabel, statusLabel } from "./reminder-labels";

const object = (value: unknown): value is Record<string, unknown> =>
    !!value && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown) => (typeof value === "string" ? value : "");
const strings = (value: unknown): string[] =>
    Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];

export function ReminderEvidence({
    value,
    locale,
}: {
    value: Record<string, unknown>;
    locale: Locale;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const evaluation = object(value.evaluation) ? value.evaluation : null;
    const rule = object(evaluation?.rule) ? evaluation.rule : null;
    const inputs = object(evaluation?.inputs) ? evaluation.inputs : null;
    const manual = text(value.manual_due_date),
        calculated = text(value.calculated_date);
    const status = text(value.evaluation_status),
        effective = manual || (status === "calculated" ? calculated : "");
    return (
        <div className="reminder-evidence">
            <dl className="reminder-dates">
                <div>
                    <dt>{t("采用日期", "Effective date")}</dt>
                    <dd>{effective || t("未确定", "Not determined")}</dd>
                </div>
                <div>
                    <dt>{t("系统推算日期", "Calculated date")}</dt>
                    <dd>{calculated || "—"}</dd>
                </div>
                <div>
                    <dt>{t("人工日期", "Manual date")}</dt>
                    <dd>{manual || "—"}</dd>
                </div>
            </dl>
            <p>
                {t("自动推算状态", "Calculation status")}: {statusLabel(status, locale)}
            </p>
            {manual && (
                <p className="help-text">
                    {t("人工日期优先；理由", "Manual date takes precedence; reason")}:{" "}
                    {text(value.manual_reason)}
                </p>
            )}
            {strings(evaluation?.reasons).map((reason) => (
                <p key={reason} className="help-text">
                    {reasonLabel(reason, locale)}
                </p>
            ))}
            {strings(evaluation?.missing).length > 0 && (
                <p className="help-text">
                    {t("待补充", "Missing")}:{" "}
                    {strings(evaluation?.missing)
                        .map((key) => parameterLabel(key, locale))
                        .join(" · ")}
                </p>
            )}
            {rule && (
                <details className="help-text">
                    <summary>{t("查看本次推算依据", "View captured calculation evidence")}</summary>
                    <p>
                        {ruleLabel(text(rule.id), locale)} · {text(rule.version)}
                    </p>
                    <p>
                        {t("规则核验日期", "Rule checked on")}: {text(rule.checked_on)}
                    </p>
                    <ul>
                        {strings(rule.sources).map((source) => (
                            <li key={source}>
                                {source.startsWith("https://") ? (
                                    <a href={source} target="_blank" rel="noopener noreferrer">
                                        {source}
                                    </a>
                                ) : source === "user_verified_certificate_expiry" ? (
                                    t(
                                        "本人确认的证件记载日期",
                                        "Expiry date verified from the certificate",
                                    )
                                ) : (
                                    source
                                )}
                            </li>
                        ))}
                    </ul>
                    {inputs && (
                        <dl>
                            {Object.entries(inputs)
                                .filter(([, item]) => item !== null)
                                .map(([key, item]) => (
                                    <div key={key}>
                                        <dt>{parameterLabel(key, locale)}</dt>
                                        <dd>
                                            {typeof item === "boolean"
                                                ? item
                                                    ? t("已确认适用", "Confirmed applicable")
                                                    : t("已确认不适用", "Confirmed not applicable")
                                                : typeof item === "string" ||
                                                    typeof item === "number"
                                                  ? String(item)
                                                  : "—"}
                                        </dd>
                                    </div>
                                ))}
                        </dl>
                    )}
                </details>
            )}
        </div>
    );
}
