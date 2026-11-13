import { ApiError } from "./api";
import type { CreditHistoryEntry } from "./credit-api";
import { creditHistoryDisplay } from "./credit-revision-history-display";
import type { CreditHistoryScope, CreditHistoryValue } from "./credit-revision-history-display";
import type { Locale } from "./i18n";
import { formatAmount } from "./money";
import "./credit-form.css";

export type BusinessCreditRevisionHistoryProps = CreditHistoryScope & {
    locale: Locale;
    available: boolean;
    requestKey: string;
    offset: number;
    selected: CreditHistoryEntry | null;
    loaded: { key: string; value: CreditHistoryValue } | null;
    failure: { key: string; error: unknown } | null;
    onSelect: (entry: CreditHistoryEntry | null) => void;
    onPage: (offset: number) => void;
    onReload: () => void;
};

/** Read-only view; the caller owns scoped GET cancellation and pending-command isolation. */
export function BusinessCreditRevisionHistory(props: BusinessCreditRevisionHistoryProps) {
    const { locale, selected, offset, onSelect, onPage, onReload } = props;
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const failure = props.failure?.key === props.requestKey ? props.failure.error : null;
    if (!props.available || (failure instanceof ApiError && failure.status === 401))
        return (
            <p role="alert">
                {t("请登录后重新读取贷项历史。", "Sign in to reload credit history.")}
            </p>
        );
    const loaded = props.loaded?.key === props.requestKey ? props.loaded : null;
    const data =
        failure || !loaded ? null : creditHistoryDisplay(loaded.value, props, offset, selected);
    const invalid = !!loaded && !data;
    const action = (value: CreditHistoryEntry["action"]) =>
        value === "create"
            ? t("原创建", "Original creation")
            : value === "correct"
              ? t("更正", "Correction")
              : t("取消", "Cancellation");
    const money = (value: string) => `${formatAmount(value, locale)} ${props.assetId}`;
    const fact = (
        title: string,
        value: { net: string; tax: string; total: string; date: string },
    ) => (
        <div>
            <strong>{title}</strong>
            {[
                [t("净额", "Net"), value.net],
                [t("税额", "Tax"), value.tax],
                [t("含税额", "Gross"), value.total],
            ].map(([label, amount]) => (
                <p key={label}>
                    {label}: {money(amount)}
                </p>
            ))}
            <p>
                {t("归属日", "Recognition date")}: {value.date}
            </p>
        </div>
    );
    return (
        <section className="credit-form" aria-label={t("贷项版本历史", "Credit version history")}>
            <h3>{t("贷项版本历史", "Credit version history")}</h3>
            <p>
                {t(
                    "这里是已保存的固定历史，不代表今日当前状态，也不能确认未知提交。未知结果仍须用原请求重试。",
                    "These are fixed saved facts, not today's current state or proof of an unknown submission. Retry an unknown outcome using its original request.",
                )}
            </p>
            {(failure || invalid) && (
                <p role="alert">
                    {t(
                        "无法安全读取此页历史，请重新读取。此操作不会重试或更改待确认的提交。",
                        "This history could not be read safely. Reload it; this does not retry or change any pending submission.",
                    )}
                </p>
            )}
            {!loaded && !failure && <p role="status">{t("读取中…", "Loading…")}</p>}
            {data?.kind === "page" && (
                <>
                    {data.entries.length === 0 && (
                        <p>{t("本页无版本记录。", "No versions on this page.")}</p>
                    )}
                    <ul className="master-records">
                        {data.entries.map((row) => (
                            <li key={row.version}>
                                <strong>
                                    {t("组版本", "Group version")}: {row.version} ·{" "}
                                    {action(row.action)}
                                </strong>
                                <span>{row.created_at}</span>
                                <span>{row.reason}</span>
                                <button type="button" onClick={() => onSelect(row)}>
                                    {t("查看固定详情", "View fixed details")}
                                </button>
                            </li>
                        ))}
                    </ul>
                    <div className="form-actions">
                        <button
                            type="button"
                            disabled={offset === 0}
                            onClick={() => onPage(offset - 25)}
                        >
                            {t("上一页", "Previous page")}
                        </button>
                        <button
                            type="button"
                            disabled={data.entries.length !== 25 || offset >= 100000}
                            onClick={() => onPage(offset + 25)}
                        >
                            {t("下一页", "Next page")}
                        </button>
                    </div>
                </>
            )}
            {data?.kind === "detail" && (
                <>
                    <h4>
                        {action(data.action)} · {t("组版本", "Group version")}: {data.version}
                    </h4>
                    <dl className="confirmation-facts">
                        <dt>{t("固定回执标识", "Fixed receipt ID")}</dt>
                        <dd>{data.receiptId}</dd>
                        <dt>{t("原单据标识", "Original document ID")}</dt>
                        <dd>{props.documentId}</dd>
                        {data.previousVersion !== null && (
                            <>
                                <dt>{t("前组版本", "Previous group version")}</dt>
                                <dd>{data.previousVersion}</dd>
                            </>
                        )}
                        <dt>
                            {data.action === "create"
                                ? t("创建说明", "Creation reason")
                                : t("修订原因", "Revision reason")}
                        </dt>
                        <dd>{data.reason}</dd>
                        {data.previousDate && (
                            <>
                                <dt>{t("前版日期与说明", "Previous date and reason")}</dt>
                                <dd>
                                    {data.previousDate} · {data.previousReason}
                                </dd>
                            </>
                        )}
                        {data.creditDate && (
                            <>
                                <dt>{t("本版日期与说明", "This version's date and reason")}</dt>
                                <dd>
                                    {data.creditDate} · {data.creditReason}
                                </dd>
                            </>
                        )}
                    </dl>
                    <ul className="master-records">
                        {data.lines.map((line) => (
                            <li key={line.source}>
                                <strong>
                                    {line.action === "create"
                                        ? t("新增成员", "Added member")
                                        : line.action === "correct"
                                          ? t("保留并更正", "Retained and corrected")
                                          : t("移除成员", "Removed member")}
                                </strong>
                                <span>
                                    {t("源行", "Source line")}: {line.source}
                                </span>
                                <span>
                                    {t("行操作版本", "Member operation version")}: {line.version}
                                </span>
                                {line.previousVersion !== null && (
                                    <span>
                                        {t("前行操作版本", "Previous member operation version")}:{" "}
                                        {line.previousVersion}
                                    </span>
                                )}
                                {line.before && fact(t("前版事实", "Previous fact"), line.before)}
                                {line.after ? (
                                    fact(t("本版事实", "This version's fact"), line.after)
                                ) : (
                                    <p>
                                        {t(
                                            "仅冲销前版，无零金额替代；不自动产生真实退款。",
                                            "Only the previous fact is reversed; no zero-amount replacement or automatic cash refund is recorded.",
                                        )}
                                    </p>
                                )}
                                <details>
                                    <summary>
                                        {t("来源与凭证标识", "Source and posting identities")}
                                    </summary>
                                    <p>
                                        {t("源操作", "Source operation")}: {line.sourceOperation}
                                    </p>
                                    <p>
                                        {t("行操作", "Member operation")}: {line.operation}
                                    </p>
                                    {[
                                        [t("前版凭证", "Previous posting"), line.previousJournal],
                                        [t("冲销凭证", "Reversal posting"), line.reversalJournal],
                                        [t("本版凭证", "This version's posting"), line.journal],
                                    ].map(
                                        ([label, journal]) =>
                                            journal && (
                                                <p key={label}>
                                                    {label}: {journal}
                                                </p>
                                            ),
                                    )}
                                </details>
                            </li>
                        ))}
                    </ul>
                </>
            )}
            <div className="form-actions">
                <button type="button" onClick={onReload}>
                    {t("重新读取历史", "Reload history")}
                </button>
                {selected && (
                    <button type="button" onClick={() => onSelect(null)}>
                        {t("返回版本目录", "Back to versions")}
                    </button>
                )}
            </div>
        </section>
    );
}
