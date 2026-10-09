import { useId, useState } from "react";
import type { FormEvent } from "react";
import type { Locale } from "./i18n";
import type { DraftFields, DraftReferences } from "./draft-fields";
import { newDraftLine, validDraftFields } from "./draft-fields";
import { DraftLineFields } from "./DraftLineFields";
import { priceBusinessDocument } from "./business-pricing";
import { formatAmount } from "./money";
import "./draft-form.css";

export function DraftForm({
    initial,
    refs,
    locale,
    existing,
    locked,
    canFinish,
    onSave,
    onCancel,
}: {
    initial: DraftFields;
    refs: DraftReferences;
    locale: Locale;
    existing: boolean;
    locked: boolean;
    canFinish: boolean;
    onSave: (fields: DraftFields, refreshSnapshots: boolean) => void;
    onCancel: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const id = useId();
    const [fields, setFields] = useState(initial);
    const [refreshSnapshots, setRefreshSnapshots] = useState(false);
    const [invalid, setInvalid] = useState(false);
    const asset = refs.assets.find((row) => row.asset_id === fields.asset_id);
    const role = fields.document_kind === "invoice" ? "customer" : "supplier";
    const parties = refs.parties.filter(
        (row) => !row.archived && row.name && (row.role === role || row.role === "both"),
    );
    const party = refs.parties.find((row) => row.id === fields.party_id);
    let price = null;
    try {
        if (asset) price = priceBusinessDocument(fields.lines, asset.scale);
    } catch {
        /* Keep invalid raw input for correction. */
    }
    function submit(event: FormEvent) {
        event.preventDefault();
        if (locked) return;
        if (!validDraftFields(fields, refs)) {
            setInvalid(true);
            return;
        }
        setInvalid(false);
        onSave(fields, refreshSnapshots);
    }
    return (
        <form onSubmit={submit} aria-label={t("经营草稿表单", "Business draft form")}>
            <fieldset disabled={locked} className="draft-fields">
                <legend>{t("草稿内容", "Draft contents")}</legend>
                <div className="form-grid">
                    <div className="field">
                        <label htmlFor={`${id}-kind`}>{t("单据类型", "Document type")}</label>
                        <select
                            id={`${id}-kind`}
                            value={fields.document_kind}
                            onChange={(event) =>
                                setFields({
                                    ...fields,
                                    document_kind: event.target.value as "invoice" | "bill",
                                })
                            }
                        >
                            <option value="invoice">{t("销售 Invoice", "Sales invoice")}</option>
                            <option value="bill">{t("采购账单", "Purchase bill")}</option>
                        </select>
                    </div>
                    <div className="field">
                        <label htmlFor={`${id}-party`}>{t("往来单位", "Business party")}</label>
                        <select
                            id={`${id}-party`}
                            required
                            value={fields.party_id}
                            onChange={(event) =>
                                setFields({ ...fields, party_id: event.target.value })
                            }
                        >
                            <option value="">{t("请选择", "Choose")}</option>
                            {fields.party_id &&
                                !parties.some((row) => row.id === fields.party_id) && (
                                    <option value={fields.party_id} disabled>
                                        {party?.name ?? fields.party_id} ·{" "}
                                        {t("不可用于保存", "Unavailable for saving")}
                                    </option>
                                )}
                            {parties.map((row) => (
                                <option key={row.id} value={row.id}>
                                    {row.name}
                                </option>
                            ))}
                        </select>
                    </div>
                    <div className="field">
                        <label htmlFor={`${id}-asset`}>{t("计价资产", "Pricing asset")}</label>
                        <select
                            id={`${id}-asset`}
                            required
                            value={fields.asset_id}
                            onChange={(event) =>
                                setFields({ ...fields, asset_id: event.target.value })
                            }
                        >
                            <option value="">{t("请选择", "Choose")}</option>
                            {fields.asset_id && (!asset || !asset.enabled) && (
                                <option value={fields.asset_id} disabled>
                                    {fields.asset_id} · {t("已停用", "Disabled")}
                                </option>
                            )}
                            {refs.assets
                                .filter((row) => row.enabled)
                                .map((row) => (
                                    <option key={row.asset_id} value={row.asset_id}>
                                        {row.asset_id} ({row.scale})
                                    </option>
                                ))}
                        </select>
                    </div>
                    <div className="field">
                        <label htmlFor={`${id}-issue`}>{t("开单日期", "Issue date")}</label>
                        <input
                            id={`${id}-issue`}
                            type="date"
                            required
                            min="0001-01-01"
                            max="9999-12-31"
                            value={fields.issue_date}
                            onChange={(event) =>
                                setFields({ ...fields, issue_date: event.target.value })
                            }
                        />
                    </div>
                    <div className="field">
                        <label htmlFor={`${id}-due`}>
                            {t("到期日期（可选）", "Due date (optional)")}
                        </label>
                        <input
                            id={`${id}-due`}
                            type="date"
                            min="0001-01-01"
                            max="9999-12-31"
                            value={fields.due_date ?? ""}
                            onChange={(event) =>
                                setFields({ ...fields, due_date: event.target.value || null })
                            }
                        />
                    </div>
                    <div className="field">
                        <label htmlFor={`${id}-notes`}>{t("备注", "Notes")}</label>
                        <textarea
                            id={`${id}-notes`}
                            maxLength={2000}
                            value={fields.notes ?? ""}
                            onChange={(event) =>
                                setFields({ ...fields, notes: event.target.value || null })
                            }
                        />
                    </div>
                </div>
                <p className="help-text">
                    {t(
                        "金额按每行先舍入未税净额，再计算税额。折扣为整行固定金额；更换资产不会换算金额。",
                        "Each line rounds its net amount before calculating tax. Discounts are fixed line amounts. Changing the asset does not convert amounts.",
                    )}
                </p>
                {fields.lines.map((line, index) => (
                    <DraftLineFields
                        key={line.id}
                        value={line}
                        index={index}
                        kind={fields.document_kind}
                        refs={refs}
                        locale={locale}
                        onChange={(next) =>
                            setFields({
                                ...fields,
                                lines: fields.lines.map((row) => (row.id === line.id ? next : row)),
                            })
                        }
                        onRemove={() =>
                            setFields({
                                ...fields,
                                lines: fields.lines.filter((row) => row.id !== line.id),
                            })
                        }
                    />
                ))}
                <button
                    type="button"
                    disabled={fields.lines.length >= 200}
                    onClick={() =>
                        setFields({
                            ...fields,
                            lines: [
                                ...fields.lines,
                                newDraftLine(crypto.randomUUID(), fields.issue_date),
                            ],
                        })
                    }
                >
                    {t("添加明细行", "Add line")}
                </button>
                {existing && (
                    <label className="check-row">
                        <input
                            type="checkbox"
                            checked={refreshSnapshots}
                            onChange={(event) => setRefreshSnapshots(event.target.checked)}
                        />
                        {t(
                            "保存时刷新公司、往来及分类/项目快照",
                            "Refresh issuer, party and category/project snapshots when saving",
                        )}
                    </label>
                )}
            </fieldset>
            <div className="draft-totals" aria-live="polite">
                {price ? (
                    <dl>
                        {(
                            [
                                ["net_amount", "未税合计", "Net total"],
                                ["tax_amount", "税额合计", "Tax total"],
                                ["total_amount", "含税合计", "Total including tax"],
                            ] as const
                        ).map(([key, zh, en]) => (
                            <div key={key}>
                                <dt>{t(zh, en)}</dt>
                                <dd>
                                    {formatAmount(price[key], locale)} {fields.asset_id}
                                </dd>
                            </div>
                        ))}
                    </dl>
                ) : (
                    <p>
                        {t(
                            "请选择资产并核对每行的精确数量、单价、折扣和税率。",
                            "Choose an asset and check each line's exact quantity, price, discount and tax rate.",
                        )}
                    </p>
                )}
            </div>
            {invalid && (
                <p role="alert">
                    {t(
                        "请核对有效往来、分类/项目、日期、说明和金额；草稿可暂不添加明细行。",
                        "Check active party, category/project, dates, description and amounts. Drafts may have no lines yet.",
                    )}
                </p>
            )}
            <div className="form-actions">
                <button type="button" disabled={!canFinish} onClick={onCancel}>
                    {t("返回草稿列表", "Back to drafts")}
                </button>
                <button type="submit" className="primary-button" disabled={locked}>
                    {t("保存草稿", "Save draft")}
                </button>
            </div>
        </form>
    );
}
