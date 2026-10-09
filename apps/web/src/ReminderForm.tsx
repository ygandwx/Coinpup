import { useId, useState } from "react";
import type { FormEvent } from "react";
import type { Locale } from "./i18n";
import type { ReminderRule } from "./reminder-api";
import type { ReminderFields } from "./reminder-fields";
import { reminderRuleKey } from "./reminder-fields";
import { ruleLabel } from "./reminder-labels";
import "./draft-form.css";
import "./reminder-form.css";

export type ReminderEditorMode = "create" | "save" | "recalculate" | "manual" | "clear";
export function ReminderForm({
    initial,
    mode,
    rules,
    locale,
    locked,
    canFinish,
    onSave,
    onCancel,
}: {
    initial: ReminderFields;
    mode: ReminderEditorMode;
    rules: ReminderRule[];
    locale: Locale;
    locked: boolean;
    canFinish: boolean;
    onSave: (fields: ReminderFields) => void;
    onCancel: () => void;
}) {
    const id = useId(),
        t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [fields, setFields] = useState(initial);
    const change = (patch: Partial<ReminderFields>) =>
        setFields((current) => ({ ...current, ...patch }));
    const meta = mode === "create" || mode === "save",
        calculation = mode === "create" || mode === "recalculate";
    const manual = mode === "create" || mode === "manual" || mode === "clear";
    const selected = rules.find((rule) => reminderRuleKey(rule) === fields.ruleKey);
    function submit(event: FormEvent) {
        event.preventDefault();
        if (!locked) onSave(fields);
    }
    const actionLabel =
        mode === "create"
            ? t("创建事项", "Create event")
            : mode === "save"
              ? t("保存资料", "Save details")
              : mode === "recalculate"
                ? t("按所选规则重算", "Recalculate selected rule")
                : mode === "clear"
                  ? t("清除人工日期", "Clear manual date")
                  : t("设置人工日期", "Set manual date");
    return (
        <form
            className="reminder-form"
            aria-label={t("提醒事项表单", "Reminder event form")}
            onSubmit={submit}
        >
            <fieldset disabled={locked} className="draft-fields">
                <legend>{actionLabel}</legend>
                <div className="form-grid">
                    {mode === "create" && (
                        <div className="field">
                            <label htmlFor={`${id}-kind`}>{t("事项类型", "Event kind")}</label>
                            <select
                                id={`${id}-kind`}
                                value={fields.kind}
                                onChange={(event) =>
                                    change({
                                        kind: event.target.value as ReminderFields["kind"],
                                        ruleKey: "",
                                    })
                                }
                            >
                                <option value="annual">
                                    {t("年审／年度报告", "Annual filing")}
                                </option>
                                <option value="tax">{t("税务申报", "Tax filing")}</option>
                                <option value="certificate">
                                    {t("证件到期", "Certificate expiry")}
                                </option>
                            </select>
                        </div>
                    )}
                    {meta && (
                        <>
                            <div className="field">
                                <label htmlFor={`${id}-title`}>
                                    {t("事项标题", "Event title")}
                                </label>
                                <input
                                    id={`${id}-title`}
                                    required
                                    maxLength={160}
                                    value={fields.title}
                                    onChange={(event) => change({ title: event.target.value })}
                                />
                            </div>
                            <div className="field field-wide">
                                <label htmlFor={`${id}-notes`}>{t("说明", "Notes")}</label>
                                <textarea
                                    id={`${id}-notes`}
                                    rows={3}
                                    maxLength={2000}
                                    value={fields.notes}
                                    onChange={(event) => change({ notes: event.target.value })}
                                />
                            </div>
                        </>
                    )}
                    {calculation && (
                        <>
                            <div className="field field-wide">
                                <label htmlFor={`${id}-rule`}>
                                    {t("推算规则", "Calculation rule")}
                                </label>
                                <select
                                    id={`${id}-rule`}
                                    required={mode === "recalculate"}
                                    value={fields.ruleKey}
                                    onChange={(event) => change({ ruleKey: event.target.value })}
                                >
                                    <option value="">
                                        {t("不选规则／手动事项", "No rule / manual event")}
                                    </option>
                                    {fields.ruleKey && !selected && (
                                        <option value={fields.ruleKey} disabled>
                                            {t(
                                                "原规则版本不可用，请明确选择",
                                                "Captured rule unavailable; choose explicitly",
                                            )}
                                        </option>
                                    )}
                                    {rules
                                        .filter((rule) =>
                                            fields.kind === "certificate"
                                                ? rule.id === "certificate.expiry"
                                                : fields.kind === "annual"
                                                  ? rule.id !== "certificate.expiry"
                                                  : false,
                                        )
                                        .map((rule) => (
                                            <option
                                                key={reminderRuleKey(rule)}
                                                value={reminderRuleKey(rule)}
                                            >
                                                {ruleLabel(rule.id, locale)} · {rule.version}
                                                {rule.formula === "unverified"
                                                    ? t("（待核验）", " (unverified)")
                                                    : ""}
                                            </option>
                                        ))}
                                </select>
                            </div>
                            {selected && (
                                <>
                                    <div className="field">
                                        <label htmlFor={`${id}-applicability`}>
                                            {t("本事项是否适用", "Applicability to this event")}
                                        </label>
                                        <select
                                            id={`${id}-applicability`}
                                            value={fields.applicability}
                                            onChange={(event) =>
                                                change({
                                                    applicability: event.target
                                                        .value as ReminderFields["applicability"],
                                                })
                                            }
                                        >
                                            <option value="">
                                                {t(
                                                    "尚待本人／会计确认",
                                                    "Awaiting my / accountant confirmation",
                                                )}
                                            </option>
                                            <option value="yes">
                                                {t("已确认适用", "Confirmed applicable")}
                                            </option>
                                            <option value="no">
                                                {t("已确认不适用", "Confirmed not applicable")}
                                            </option>
                                        </select>
                                    </div>
                                    {selected.required.includes("filing_year") && (
                                        <div className="field">
                                            <label htmlFor={`${id}-year`}>
                                                {t("申报年度", "Filing year")}
                                            </label>
                                            <input
                                                id={`${id}-year`}
                                                inputMode="numeric"
                                                pattern="[0-9]{1,4}"
                                                value={fields.filingYear}
                                                onChange={(event) =>
                                                    change({ filingYear: event.target.value })
                                                }
                                            />
                                        </div>
                                    )}
                                    {selected.required.includes("period_end") && (
                                        <div className="field">
                                            <label htmlFor={`${id}-period`}>
                                                {t(
                                                    "实际财务年度结束日",
                                                    "Actual financial year end",
                                                )}
                                            </label>
                                            <input
                                                id={`${id}-period`}
                                                type="date"
                                                value={fields.periodEnd}
                                                onChange={(event) =>
                                                    change({ periodEnd: event.target.value })
                                                }
                                            />
                                        </div>
                                    )}
                                    {selected.required.includes("expiry_date") && (
                                        <div className="field">
                                            <label htmlFor={`${id}-expiry`}>
                                                {t("证件记载到期日", "Expiry date on certificate")}
                                            </label>
                                            <input
                                                id={`${id}-expiry`}
                                                type="date"
                                                value={fields.expiryDate}
                                                onChange={(event) =>
                                                    change({ expiryDate: event.target.value })
                                                }
                                            />
                                        </div>
                                    )}
                                    <p className="help-text field-wide">
                                        {t(
                                            "注册资料由账本主体读取；缺少参数或规则尚未核验时不会采用推算期限。",
                                            "Registration details come from the ledger entity. Missing inputs or unverified rules do not produce an adopted deadline.",
                                        )}
                                    </p>
                                    <p className="help-text field-wide">
                                        {t("规则核验日期", "Rule checked on")}:{" "}
                                        {selected.checked_on}
                                    </p>
                                </>
                            )}
                        </>
                    )}
                    {manual && (
                        <>
                            {mode !== "clear" && (
                                <div className="field">
                                    <label htmlFor={`${id}-manual`}>
                                        {t("人工日期", "Manual date")}
                                    </label>
                                    <input
                                        id={`${id}-manual`}
                                        type="date"
                                        required={mode === "manual"}
                                        value={fields.manualDate}
                                        onChange={(event) =>
                                            change({ manualDate: event.target.value })
                                        }
                                    />
                                </div>
                            )}
                            <div className="field field-wide">
                                <label htmlFor={`${id}-reason`}>
                                    {t(
                                        "本次人工日期操作理由",
                                        "Reason for this manual date action",
                                    )}
                                </label>
                                <input
                                    id={`${id}-reason`}
                                    required={mode !== "create" || !!fields.manualDate}
                                    maxLength={500}
                                    value={fields.manualReason}
                                    onChange={(event) =>
                                        change({ manualReason: event.target.value })
                                    }
                                />
                            </div>
                            {mode === "clear" && (
                                <p className="help-text field-wide">
                                    {t(
                                        "清除覆盖后仅采用当前已确认的推算值；推算仍待确认时将没有有效日期。",
                                        "Clearing the override uses only a current calculated date. An unverified calculation leaves no effective date.",
                                    )}
                                </p>
                            )}
                        </>
                    )}
                </div>
            </fieldset>
            <div className="form-actions">
                <button type="submit" className="primary-button" disabled={locked}>
                    {actionLabel}
                </button>
                <button
                    type="button"
                    className="secondary-button"
                    disabled={!canFinish}
                    onClick={onCancel}
                >
                    {t("取消", "Cancel")}
                </button>
            </div>
        </form>
    );
}
