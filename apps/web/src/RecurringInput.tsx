import type { Locale } from "./i18n";
import type { BusinessDraftCreate } from "./business-draft-api";

export function RecurringInput({
    value,
    locale,
    template = false,
}: {
    value: BusinessDraftCreate;
    locale: Locale;
    template?: boolean;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    return (
        <details className="help-text">
            <summary>
                {template
                    ? t("已捕获模板内容", "Captured template input")
                    : t("原生成内容", "Original generated input")}
            </summary>
            <p>
                {t("开具日期", "Issue date")}: {value.issue_date} · {t("到期日", "Due date")}:{" "}
                {value.due_date ?? "—"} · {value.asset_id}
            </p>
            {value.notes && <p>{value.notes}</p>}
            <ul>
                {value.lines?.map((line) => (
                    <li key={line.id}>
                        {line.description}: {line.quantity} × {line.unit_price} {value.asset_id};{" "}
                        {t("折扣", "Discount")} {line.discount_amount ?? "0"};{" "}
                        {t("税率", "Tax rate")} {line.tax_rate_percent ?? "0"}%;{" "}
                        {t("归属日", "Recognition date")} {line.recognition_date}
                    </li>
                ))}
            </ul>
        </details>
    );
}
