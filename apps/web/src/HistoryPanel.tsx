import { useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import { businessError } from "./business-errors";
import type { Locale } from "./i18n";
import { getOperationHistory } from "./ledger-api";
import type { Account, Category, HistoryEntry, JournalAudit } from "./ledger-api";

function JournalDetails({
    journal,
    accounts,
    categories,
    locale,
}: {
    journal: JournalAudit;
    accounts: Account[];
    categories: Category[];
    locale: Locale;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const categoryName = (id: string) => {
        const value = categories.find((item) => item.id === id);
        return value ? (locale === "en" ? value.name_en || value.name : value.name) : id;
    };
    const roles: Record<string, string> = {
        account: t("账户变动", "Account movement"),
        income: t("收入", "Income"),
        expense: t("支出", "Expense"),
        opening: t("期初对应项", "Opening counterpart"),
        equity: t("期初对应项", "Opening counterpart"),
        exchange: t("换汇对应项", "Exchange counterpart"),
    };
    return (
        <section className={`journal-detail ${journal.kind}`}>
            <h4>
                {journal.kind === "reversal"
                    ? t("冲销原记录", "Reversal of previous entry")
                    : t("入账内容", "Posted entry")}
            </h4>
            <p className="help-text">
                {t("交易日期", "Transaction date")}: {journal.transaction_date} ·{" "}
                {t("业务归属日", "Recognition date")}: {journal.recognition_date}
            </p>
            {journal.description && <p className="operation-description">{journal.description}</p>}
            <ul className="journal-lines">
                {journal.lines.map((line) => (
                    <li key={line.id}>
                        <div>
                            <strong>
                                {line.account_id
                                    ? (accounts.find((item) => item.id === line.account_id)?.name ??
                                      line.account_id)
                                    : line.category_id
                                      ? categoryName(line.category_id)
                                      : (roles[line.role] ?? line.role)}
                            </strong>
                            <p className="help-text">
                                {roles[line.role] ?? line.role}
                                {line.component_no > 0 && (
                                    <>
                                        {" "}
                                        · {t("手续费", "Fee")} {line.component_no}
                                    </>
                                )}{" "}
                                · {line.asset_id}
                            </p>
                        </div>
                        <span
                            className="money-quantity"
                            tabIndex={0}
                            aria-label={`${line.asset_id} ${line.amount}`}
                        >
                            {line.amount}
                        </span>
                    </li>
                ))}
            </ul>
            <details className="journal-reference">
                <summary>{t("凭证标识", "Journal references")}</summary>
                <p>{journal.id}</p>
                {journal.reverses_journal_id && (
                    <p>
                        {t("冲销目标", "Reverses")}: {journal.reverses_journal_id}
                    </p>
                )}
            </details>
        </section>
    );
}

export function HistoryPanel({
    locale,
    ledgerId,
    operationId,
    accounts,
    categories,
    onUnauthorized,
    onClose,
}: {
    locale: Locale;
    ledgerId: string;
    operationId: string;
    accounts: Account[];
    categories: Category[];
    onUnauthorized: () => void;
    onClose: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [entries, setEntries] = useState<HistoryEntry[]>([]);
    const [offset, setOffset] = useState(0);
    const [refresh, setRefresh] = useState(0);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<unknown>(null);
    const onUnauthorizedRef = useRef(onUnauthorized);
    onUnauthorizedRef.current = onUnauthorized;
    useEffect(() => {
        document.getElementById("history-panel-title")?.focus();
    }, []);
    useEffect(() => {
        const abort = new AbortController();
        setLoading(true);
        setError(null);
        void getOperationHistory(ledgerId, operationId, { limit: 10, offset }, abort.signal)
            .then((items) => {
                if (abort.signal.aborted) return;
                setEntries(items);
                setLoading(false);
            })
            .catch((problem) => {
                if (abort.signal.aborted) return;
                setLoading(false);
                setEntries([]);
                if (problem instanceof ApiError && problem.kind === "unauthorized")
                    onUnauthorizedRef.current();
                else setError(problem);
            });
        return () => abort.abort();
    }, [ledgerId, operationId, offset, refresh]);
    return (
        <section className="business-panel history-panel">
            <div className="section-heading">
                <div>
                    <h2 id="history-panel-title" tabIndex={-1}>
                        {t("版本历史", "Version history")}
                    </h2>
                    <p className="help-text">
                        {t(
                            "按版本保留入账、冲销和修改原因；原记录不会被覆盖。",
                            "Posting, reversals and reasons are retained by version. Earlier entries are preserved.",
                        )}
                    </p>
                </div>
                <button className="secondary-button" onClick={onClose}>
                    {t("返回流水", "Back to transactions")}
                </button>
            </div>
            <button
                className="text-button"
                disabled={loading}
                onClick={() => setRefresh((value) => value + 1)}
            >
                {t("刷新历史", "Refresh history")}
            </button>
            {loading && (
                <p className="help-text" role="status">
                    {t("正在读取历史…", "Loading history…")}
                </p>
            )}
            {error !== null && (
                <p className="inline-error" role="alert">
                    {businessError(error, locale)}
                </p>
            )}
            {!loading &&
                entries.map((entry) => (
                    <article
                        className="history-version"
                        key={entry.version}
                        data-testid={`version-${entry.version}`}
                    >
                        <header>
                            <h3>
                                {t("版本", "Version")} {entry.version} ·{" "}
                                {entry.action === "create"
                                    ? t("首次入账", "Created")
                                    : entry.action === "correct"
                                      ? t("更正", "Corrected")
                                      : t("取消", "Cancelled")}
                            </h3>
                            <p className="help-text">
                                <time dateTime={entry.recorded_at}>
                                    {new Date(entry.recorded_at).toLocaleString(
                                        locale === "zh" ? "zh-CN" : "en-GB",
                                    )}
                                </time>
                            </p>
                        </header>
                        {entry.reason && (
                            <p className="revision-reason">
                                <strong>{t("修订原因", "Revision reason")}: </strong>
                                {entry.reason}
                            </p>
                        )}
                        <details className="journal-reference">
                            <summary>{t("执行人标识", "Actor ID")}</summary>
                            <p>{entry.actor_id}</p>
                        </details>
                        {entry.journals.map((journal) => (
                            <JournalDetails
                                key={journal.id}
                                journal={journal}
                                locale={locale}
                                accounts={accounts}
                                categories={categories}
                            />
                        ))}
                    </article>
                ))}
            {!loading && !error && !entries.length && (
                <p className="empty-copy">
                    {t("此页没有历史记录。", "There is no history on this page.")}
                </p>
            )}
            <nav className="transaction-pagination" aria-label={t("历史分页", "History pages")}>
                <button
                    className="secondary-button"
                    disabled={loading || offset === 0}
                    onClick={() => setOffset((value) => Math.max(0, value - 10))}
                >
                    {t("上一页", "Previous page")}
                </button>
                <span>
                    {t("第", "Page")} {offset / 10 + 1} {locale === "zh" ? "页" : ""}
                </span>
                <button
                    className="secondary-button"
                    disabled={loading || entries.length < 10 || offset >= 100000}
                    onClick={() => setOffset((value) => value + 10)}
                >
                    {t("下一页", "Next page")}
                </button>
            </nav>
        </section>
    );
}
