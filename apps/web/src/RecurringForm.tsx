import { useId, useState } from "react";
import type { FormEvent } from "react";
import type { Locale } from "./i18n";
import type { BusinessDraftSummary } from "./business-draft-api";
import type { RecurringFields } from "./recurring-fields";
import { validRecurringFields, usableRecurringSource } from "./recurring-fields";
import { RecurringSourcePicker } from "./RecurringSourcePicker";
import { formatAmount } from "./money";
import "./draft-form.css";

export function RecurringForm({
    ledgerId,
    initial,
    initialSource = null,
    existing,
    locale,
    locked,
    canFinish,
    onSave,
    onCancel,
    onUnauthorized,
}: {
    ledgerId: string;
    initial: RecurringFields;
    existing: boolean;
    locale: Locale;
    initialSource?: BusinessDraftSummary | null;
    locked: boolean;
    canFinish: boolean;
    onSave: (fields: RecurringFields, source: BusinessDraftSummary | null) => void;
    onCancel: () => void;
    onUnauthorized: () => void;
}) {
    const id = useId(),
        t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [fields, setFields] = useState(initial);
    const [source, setSource] = useState<BusinessDraftSummary | null>(initialSource);
    const [refreshTemplate, setRefreshTemplate] = useState(existing && initialSource !== null);
    const [invalid, setInvalid] = useState(false);
    const picking = !existing || refreshTemplate;
    function submit(event: FormEvent) {
        event.preventDefault();
        if (locked) return;
        if (
            !validRecurringFields(fields, existing) ||
            (picking && !usableRecurringSource(source))
        ) {
            setInvalid(true);
            return;
        }
        setInvalid(false);
        onSave(fields, picking ? source : null);
    }
    return (
        <form aria-label={t("周期规则表单", "Recurring rule form")} onSubmit={submit}>
            <fieldset disabled={locked} className="draft-fields">
                <legend>{t("规则内容", "Rule contents")}</legend>
                <div className="form-grid">
                    <div className="field">
                        <label htmlFor={`${id}-name`}>{t("规则名称", "Rule name")}</label>
                        <input
                            id={`${id}-name`}
                            required
                            maxLength={160}
                            value={fields.name}
                            onChange={(event) => setFields({ ...fields, name: event.target.value })}
                        />
                    </div>
                    <div className="field">
                        <label htmlFor={`${id}-zone`}>{t("规则时区", "Rule timezone")}</label>
                        <input
                            id={`${id}-zone`}
                            required
                            disabled={existing}
                            maxLength={100}
                            placeholder="Asia/Shanghai, Europe/Tallinn, UTC"
                            value={fields.timezone}
                            onChange={(event) =>
                                setFields({ ...fields, timezone: event.target.value })
                            }
                        />
                    </div>
                    <div className="field">
                        <label htmlFor={`${id}-anchor`}>
                            {t("首个周期日期", "First occurrence date")}
                        </label>
                        <input
                            id={`${id}-anchor`}
                            required
                            disabled={existing}
                            type="date"
                            min="0001-01-01"
                            max="9999-12-31"
                            value={fields.anchor}
                            onChange={(event) =>
                                setFields({ ...fields, anchor: event.target.value })
                            }
                        />
                    </div>
                    <div className="field">
                        <label htmlFor={`${id}-frequency`}>{t("周期单位", "Frequency")}</label>
                        <select
                            id={`${id}-frequency`}
                            disabled={existing}
                            value={fields.frequency}
                            onChange={(event) =>
                                setFields({
                                    ...fields,
                                    frequency: event.target.value as RecurringFields["frequency"],
                                })
                            }
                        >
                            <option value="day">{t("日", "Day")}</option>
                            <option value="week">{t("周", "Week")}</option>
                            <option value="month">{t("月", "Month")}</option>
                            <option value="year">{t("年", "Year")}</option>
                        </select>
                    </div>
                    <div className="field">
                        <label htmlFor={`${id}-interval`}>{t("每隔几个周期", "Interval")}</label>
                        <input
                            id={`${id}-interval`}
                            required
                            disabled={existing}
                            inputMode="numeric"
                            pattern="[1-9][0-9]{0,2}"
                            maxLength={3}
                            value={fields.interval}
                            onChange={(event) =>
                                setFields({ ...fields, interval: event.target.value })
                            }
                        />
                    </div>
                </div>
                <p className="help-text">
                    {t(
                        "间隔1–120。按原起点计算，缺少对应日的月份取月末。修改起点、周期或时区时须暂停旧规则并新建。",
                        "Use an interval of 1–120. Dates follow the original anchor, clamping to month end when needed. Pause and replace a rule to change its calendar or timezone.",
                    )}
                </p>
                {existing && (
                    <label className="archive-toggle">
                        <input
                            type="checkbox"
                            checked={refreshTemplate}
                            onChange={(event) => {
                                setRefreshTemplate(event.target.checked);
                                setSource(null);
                            }}
                        />
                        {t("显式刷新捕获模板", "Explicitly refresh captured template")}
                    </label>
                )}
                <p className="help-text">
                    {t(
                        "源草稿后续修改或归档不会自动改变此规则；每次生成只创建草稿，不入账、不编号、不发邮件。",
                        "Later edits or archival of the source draft do not change this rule automatically. Each occurrence creates only a draft, without posting, numbering or email.",
                    )}
                </p>
                {picking && (
                    <RecurringSourcePicker
                        ledgerId={ledgerId}
                        locale={locale}
                        selected={source}
                        onSelect={setSource}
                        onUnauthorized={onUnauthorized}
                    />
                )}
                {picking && source && (
                    <p role="status" data-testid="recurring-source-selected">
                        {t("已选择", "Selected")}: {String(source.party_snapshot.name ?? "—")} ·{" "}
                        {source.issue_date} · {formatAmount(source.total_amount, locale)}{" "}
                        {source.asset_id} · {t("版本", "Version")} {source.version}
                    </p>
                )}
            </fieldset>
            {invalid && (
                <p role="alert">
                    {t(
                        "请检查名称、日期、时区、间隔，并选择可用的Invoice草稿。",
                        "Check the name, date, timezone and interval, and choose an available invoice draft.",
                    )}
                </p>
            )}
            <div className="form-actions">
                <button type="submit" disabled={locked}>
                    {t("保存周期规则", "Save recurring rule")}
                </button>
                <button type="button" disabled={!canFinish} onClick={onCancel}>
                    {t("返回规则列表", "Back to rules")}
                </button>
            </div>
        </form>
    );
}
