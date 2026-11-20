import type { DocumentCurrent } from "./document-revision-api";
import type { Locale } from "./i18n";
import { formatAmount } from "./money";

/** Caller validates the complete read and authentication before displaying private facts. */
export function BusinessDocumentCurrentFacts({
    current,
    locale,
    historical = false,
}: {
    current: DocumentCurrent;
    locale: Locale;
    historical?: boolean;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const d = current.document;
    if (!d)
        return (
            <p>
                {t(
                    "此单据已取消，没有当前应计明细。",
                    "This document is cancelled and has no current accruals.",
                )}
            </p>
        );
    const amount = (v: string) => `${formatAmount(v, locale)} ${d.asset_id}`;
    return (
        <section
            aria-label={
                historical
                    ? t("原始单据事实", "Original document facts")
                    : t("当前单据事实", "Current document facts")
            }
        >
            <h4>
                {historical
                    ? t("原始单据", "Original document")
                    : t("当前单据", "Current document")}{" "}
                · {t("版本", "Version")} {current.version}
            </h4>
            <dl className="confirmation-facts">
                <dt>{t("开票主体", "Issuer")}</dt>
                <dd>{String(d.issuer_snapshot.name ?? "—")}</dd>
                <dt>{t("往来单位", "Business party")}</dt>
                <dd>{String(d.party_snapshot.name ?? d.party_id)}</dd>
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
                {d.lines.map((row) => {
                    const p = current.postings.find((p) => p.source_line_id === row.id);
                    return (
                        <li key={row.id} data-testid="document-current-line">
                            <strong>
                                {row.line_no}. {row.description}
                            </strong>
                            <span>
                                {t(
                                    "数量 × 单价；折扣；税率",
                                    "Quantity × unit price; discount; tax rate",
                                )}
                                : {row.quantity} × {row.unit_price}; {row.discount_amount};{" "}
                                {row.tax_rate_percent}%
                            </span>
                            <span>
                                {t("未税 / 税额 / 含税", "Net / Tax / Gross")}:{" "}
                                {amount(row.net_amount)} / {amount(row.tax_amount)} /{" "}
                                {amount(row.total_amount)}
                            </span>
                            <span>
                                {t("分类 / 项目", "Category / Project")}:{" "}
                                {String(row.category_snapshot.name ?? row.category_id)} /{" "}
                                {String(row.project_snapshot?.name ?? "—")}
                            </span>
                            <span>
                                {t("归属日", "Recognition date")}: {row.recognition_date}
                            </span>
                            <span>
                                {t("来源行", "Source line")}: {row.id}
                            </span>
                            <span>
                                {p
                                    ? `${t("应计 / 版本 / 凭证", "Accrual / Version / Journal")}: ${p.operation_id} / ${p.operation_version} / ${p.journal_id}`
                                    : t("零金额行，不生成分录", "Zero amount; no posting")}
                            </span>
                        </li>
                    );
                })}
            </ol>
        </section>
    );
}
