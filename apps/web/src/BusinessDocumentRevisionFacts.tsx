import type { DocumentDisplayGroup } from "./document-revision-display";
import type { Locale } from "./i18n";
import { formatAmount } from "./money";

/** Exact saved facts, or an explicitly labelled preview supplied by the validated frozen intent. */
export function BusinessDocumentRevisionFacts({
    value: d,
    locale,
    title,
}: {
    value: DocumentDisplayGroup;
    locale: Locale;
    title: string;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const amount = (v: string) => `${formatAmount(v, locale)} ${d.asset_id}`;
    return (
        <section aria-label={title}>
            <h4>{title}</h4>
            <dl className="confirmation-facts">
                <dt>{t("单据类型", "Document type")}</dt>
                <dd>
                    {d.document_kind === "invoice"
                        ? t("销售 Invoice", "Sales invoice")
                        : t("采购账单", "Purchase bill")}
                </dd>
                <dt>{t("往来身份", "Party identity")}</dt>
                <dd>{d.party_id}</dd>
                <dt>{t("开单日期 / 到期日期", "Issue date / Due date")}</dt>
                <dd>
                    {d.issue_date} / {d.due_date ?? "—"}
                </dd>
                <dt>{t("未税 / 税额 / 含税总额", "Net / Tax / Gross total")}</dt>
                <dd>
                    {amount(d.net_amount)} / {amount(d.tax_amount)} / {amount(d.total_amount)}
                </dd>
                <dt>{t("备注", "Notes")}</dt>
                <dd>{d.notes || "—"}</dd>
            </dl>
            <ol className="master-records confirmation-lines">
                {d.lines.map((row) => (
                    <li key={row.id} data-testid="document-version-line">
                        <strong>
                            {row.line_no}. {row.description}
                        </strong>
                        <span>
                            {t("来源行", "Source line")}: {row.id}
                        </span>
                        <span>
                            {t(
                                "数量 × 单价；折扣；税率",
                                "Quantity × unit price; discount; tax rate",
                            )}
                            : {row.quantity} × {row.unit_price}; {row.discount_amount ?? "0"};{" "}
                            {row.tax_rate_percent ?? "0"}%
                        </span>
                        <span>
                            {t("未税 / 税额 / 含税", "Net / Tax / Gross")}: {amount(row.net_amount)}{" "}
                            / {amount(row.tax_amount)} / {amount(row.total_amount)}
                        </span>
                        <span>
                            {t("分类 / 项目", "Category / Project")}: {row.category_id} /{" "}
                            {row.project_id ?? "—"}
                        </span>
                        <span>
                            {t("归属日", "Recognition date")}: {row.recognition_date}
                        </span>
                        {row.posting !== undefined && (
                            <span>
                                {row.posting
                                    ? `${t("应计 / 版本 / 凭证", "Accrual / Version / Journal")}: ${row.posting.operation_id} / ${row.posting.operation_version} / ${row.posting.journal_id}`
                                    : t("零金额行，不生成分录", "Zero amount; no posting")}
                            </span>
                        )}
                    </li>
                ))}
            </ol>
        </section>
    );
}
