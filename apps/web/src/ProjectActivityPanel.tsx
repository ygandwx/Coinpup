import { useEffect, useId, useRef, useState } from "react";
import { ApiError } from "./api";
import { validDraftDate } from "./draft-fields";
import type { Locale } from "./i18n";
import { formatAmount } from "./money";
import { amountFields, limit, readActivity } from "./project-activity";
import type { ActivityPage, ActivityQuery } from "./project-activity";
type Search = ActivityQuery & { sequence: number };
type Result = { key: Search; page: ActivityPage } | { key: Search; error: unknown };
export function ProjectActivityPanel({
    ledger,
    project,
    locale,
    onClose,
    onUnauthorized,
}: {
    ledger: string;
    project: { id: string; name: string };
    locale: Locale;
    onClose: () => void;
    onUnauthorized: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const id = useId();
    const [start, setStart] = useState(""),
        [end, setEnd] = useState("");
    const [search, setSearch] = useState<Search | null>(null),
        [result, setResult] = useState<Result | null>(null);
    const invalid =
        !!(start && end) && (!validDraftDate(start) || !validDraftDate(end) || start > end);
    const seq = useRef(0),
        unauthorized = useRef(onUnauthorized);
    unauthorized.current = onUnauthorized;
    function load(offset: number) {
        if (validDraftDate(start) && validDraftDate(end) && start <= end)
            setSearch({ ledger, project: project.id, start, end, offset, sequence: ++seq.current });
    }
    useEffect(() => {
        if (!search) return;
        const abort = new AbortController();
        readActivity(search, abort.signal)
            .then((page) => {
                if (!abort.signal.aborted) setResult({ key: search, page });
            })
            .catch((error: unknown) => {
                if (abort.signal.aborted) return;
                if (error instanceof ApiError && error.status === 401) unauthorized.current();
                else setResult({ key: search, error });
            });
        return () => abort.abort();
    }, [search]);
    const current = search && result?.key === search ? result : null;
    const labels = {
        income: t("经营收入", "Income"),
        cost: t("经营成本", "Cost"),
        net: t("经营结余", "Net income"),
        closing_receivable: t("期末未收", "Closing receivables"),
        closing_payable: t("期末未付", "Closing payables"),
        attributed_received: t("已归属收款", "Attributed receipts"),
        attributed_paid: t("已归属付款", "Attributed payouts"),
    };
    return (
        <section
            className="business-panel master-panel project-report"
            aria-label={t("项目收支", "Project activity")}
        >
            <div className="section-heading">
                <h2>{t("项目收支", "Project activity")}</h2>
                <button className="secondary-button" onClick={onClose}>
                    {t("返回项目", "Back to projects")}
                </button>
            </div>
            <h3>{project.name}</h3>
            <p className="help-text">
                {t(
                    "收支按所选归属日统计；未收、未付截至结束日，含以前期间的未结余额。不同资产分别显示。",
                    "Income and cost use recognition dates. Closing balances include earlier open items through the end date. Each asset is shown separately.",
                )}
            </p>
            <p className="help-text">
                {t(
                    "收付款按实际交易日及付款原币列示，仅计截至结束日已分到该项目的本金。未应用款及无项目手续费除外。",
                    "Receipts and payouts use the payment date and currency. Only principal allocated to this project by the end date is included; unapplied funds and unassigned fees are excluded.",
                )}
            </p>
            <form
                onSubmit={(e) => {
                    e.preventDefault();
                    load(0);
                }}
            >
                <div className="master-field">
                    <label htmlFor={id + "-start"}>{t("开始日期", "Start date")}</label>
                    <input
                        id={id + "-start"}
                        required
                        type="date"
                        value={start}
                        onChange={(e) => {
                            setSearch(null);
                            setStart(e.target.value);
                        }}
                    />
                </div>
                <div className="master-field">
                    <label htmlFor={id + "-end"}>{t("结束日期", "End date")}</label>
                    <input
                        id={id + "-end"}
                        required
                        type="date"
                        value={end}
                        onChange={(e) => {
                            setSearch(null);
                            setEnd(e.target.value);
                        }}
                    />
                </div>
                {invalid && (
                    <p role="alert">
                        {t(
                            "请选择有效的起止日期，开始日不能晚于结束日。",
                            "Choose valid dates. The start must not follow the end.",
                        )}
                    </p>
                )}
                <button
                    className="primary-button"
                    disabled={invalid || !start || !end}
                    type="submit"
                >
                    {t("查询收支", "Show activity")}
                </button>
            </form>
            {search && !current && <p role="status">{t("正在读取…", "Loading…")}</p>}
            {current && "error" in current && (
                <p role="alert">
                    {current.error instanceof ApiError &&
                    current.error.code === "project_activity_range"
                        ? t(
                              "合计超出可显示的精确金额范围。",
                              "A total is outside the supported exact amount range.",
                          )
                        : t(
                              "无法读取本次查询，请核对日期并重新查询。",
                              "Could not read this period. Check the dates and try again.",
                          )}
                </p>
            )}
            {current && "page" in current && (
                <>
                    <h3>
                        {current.page.start} – {current.page.end}
                    </h3>
                    {current.page.items.length === 0 && (
                        <p>
                            {t(
                                "此期间没有经营收支或期末未结余额。",
                                "No activity or closing balances for this period.",
                            )}
                        </p>
                    )}
                    <ul className="master-records">
                        {current.page.items.map((row) => (
                            <li key={row.asset_id}>
                                <h3>{row.asset_id}</h3>
                                <dl className="confirmation-facts">
                                    {amountFields.map((field) => (
                                        <div key={field}>
                                            <dt>{labels[field]}</dt>
                                            <dd>{formatAmount(row[field], locale)}</dd>
                                        </div>
                                    ))}
                                </dl>
                            </li>
                        ))}
                    </ul>
                    {(current.page.offset > 0 || current.page.has_more) && (
                        <div className="form-actions">
                            <button
                                disabled={!current.page.offset}
                                onClick={() => load(current.page.offset - limit)}
                            >
                                {t("上一页", "Previous page")}
                            </button>
                            <span>
                                {t("页", "Page")} {current.page.offset / limit + 1}
                            </span>
                            <button
                                disabled={!current.page.has_more || current.page.offset >= 100000}
                                onClick={() => load(current.page.offset + limit)}
                            >
                                {t("下一页", "Next page")}
                            </button>
                        </div>
                    )}
                </>
            )}
        </section>
    );
}
