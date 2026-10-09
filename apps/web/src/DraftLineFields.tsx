import type { DraftLine, DraftReferences } from "./draft-fields";
import type { Locale } from "./i18n";
import type { LinePrice } from "./business-pricing";
import { formatAmount } from "./money";

export function DraftLineFields({
    value,
    index,
    kind,
    refs,
    locale,
    price,
    assetId,
    onChange,
    onRemove,
}: {
    value: DraftLine;
    index: number;
    kind: "invoice" | "bill";
    refs: DraftReferences;
    locale: Locale;
    price?: LinePrice;
    assetId: string;
    onChange: (line: DraftLine) => void;
    onRemove: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const prefix = `draft-line-${value.id}`;
    const direction = kind === "invoice" ? "income" : "expense";
    const category = refs.categories.find((row) => row.id === value.category_id);
    const project = refs.projects.find((row) => row.id === value.project_id);
    const numbers = [
        ["quantity", "数量", "Quantity"],
        ["unit_price", "未税单价", "Unit price before tax"],
        ["discount_amount", "整行折扣金额", "Fixed line discount"],
        ["tax_rate_percent", "税率 (%)", "Tax rate (%)"],
    ] as const;
    return (
        <fieldset className="draft-line">
            <legend>
                {t("明细行", "Line")} {index + 1}
            </legend>
            <div className="form-grid">
                <div className="field">
                    <label htmlFor={`${prefix}-description`}>{t("说明", "Description")}</label>
                    <textarea
                        id={`${prefix}-description`}
                        required
                        maxLength={2000}
                        value={value.description}
                        onChange={(event) =>
                            onChange({ ...value, description: event.target.value })
                        }
                    />
                </div>
                {numbers.map(([key, zh, en]) => (
                    <div className="field" key={key}>
                        <label htmlFor={`${prefix}-${key}`}>{t(zh, en)}</label>
                        <input
                            id={`${prefix}-${key}`}
                            type="text"
                            inputMode="decimal"
                            required
                            maxLength={key === "quantity" || key === "tax_rate_percent" ? 39 : 40}
                            value={value[key] ?? "0"}
                            onChange={(event) => onChange({ ...value, [key]: event.target.value })}
                        />
                    </div>
                ))}
                <div className="field">
                    <label htmlFor={`${prefix}-category`}>{t("分类", "Category")}</label>
                    <select
                        id={`${prefix}-category`}
                        required
                        value={value.category_id}
                        onChange={(event) =>
                            onChange({ ...value, category_id: event.target.value })
                        }
                    >
                        <option value="">{t("请选择", "Choose")}</option>
                        {value.category_id &&
                            (!category || category.archived || category.kind !== direction) && (
                                <option value={value.category_id} disabled>
                                    {category?.name ?? value.category_id} ·{" "}
                                    {t("不可用于保存", "Unavailable for saving")}
                                </option>
                            )}
                        {refs.categories
                            .filter((row) => !row.archived && row.kind === direction)
                            .map((row) => (
                                <option key={row.id} value={row.id}>
                                    {locale === "en" ? row.name_en || row.name : row.name}
                                </option>
                            ))}
                    </select>
                </div>
                <div className="field">
                    <label htmlFor={`${prefix}-project`}>
                        {t("项目（可选）", "Project (optional)")}
                    </label>
                    <select
                        id={`${prefix}-project`}
                        value={value.project_id ?? ""}
                        onChange={(event) =>
                            onChange({ ...value, project_id: event.target.value || null })
                        }
                    >
                        <option value="">{t("无", "None")}</option>
                        {value.project_id && (!project || project.archived) && (
                            <option value={value.project_id} disabled>
                                {project?.name ?? value.project_id} · {t("已归档", "Archived")}
                            </option>
                        )}
                        {refs.projects
                            .filter((row) => !row.archived)
                            .map((row) => (
                                <option key={row.id} value={row.id}>
                                    {row.name}
                                </option>
                            ))}
                    </select>
                </div>
                <div className="field">
                    <label htmlFor={`${prefix}-date`}>{t("归属日期", "Recognition date")}</label>
                    <input
                        id={`${prefix}-date`}
                        type="date"
                        required
                        min="0001-01-01"
                        max="9999-12-31"
                        value={value.recognition_date}
                        onChange={(event) =>
                            onChange({ ...value, recognition_date: event.target.value })
                        }
                    />
                </div>
            </div>
            {price && (
                <p className="help-text">
                    {t("未税", "Net")}: {formatAmount(price.net_amount, locale)} ·{" "}
                    {t("税额", "Tax")}: {formatAmount(price.tax_amount, locale)} ·{" "}
                    {t("行合计", "Line total")}: {formatAmount(price.total_amount, locale)}{" "}
                    {assetId}
                </p>
            )}
            <button type="button" onClick={onRemove}>
                {t("移除此行", "Remove line")}
            </button>
        </fieldset>
    );
}
