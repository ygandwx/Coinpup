import { useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import type { Locale } from "./i18n";
import { listRecurringInstances } from "./recurring-api";
import type { RecurringInstance } from "./recurring-api";
import { businessError } from "./business-errors";
import { RecurringInput } from "./RecurringInput";

export function RecurringInstances({
    ledgerId,
    ruleId,
    locale,
    locked,
    onOpenDraft,
    onUnauthorized,
}: {
    ledgerId: string;
    ruleId: string;
    locale: Locale;
    locked: boolean;
    onOpenDraft: (id: string) => void;
    onUnauthorized: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [offset, setOffset] = useState(0),
        [refresh, setRefresh] = useState(0);
    const [result, setResult] = useState<{ key: string; rows: RecurringInstance[] } | null>(null);
    const [failure, setFailure] = useState<{ key: string; error: unknown } | null>(null);
    const callbacks = useRef({ onUnauthorized });
    callbacks.current = { onUnauthorized };
    const key = JSON.stringify([ledgerId, ruleId, offset, refresh]);
    const data = result?.key === key ? result.rows : null;
    const error = failure?.key === key ? failure.error : null;
    useEffect(() => {
        const abort = new AbortController();
        listRecurringInstances(ledgerId, ruleId, offset, abort.signal)
            .then((rows) => {
                if (!abort.signal.aborted) setResult({ key, rows });
            })
            .catch((problem) => {
                if (abort.signal.aborted) return;
                if (problem instanceof ApiError && problem.status === 401)
                    callbacks.current.onUnauthorized();
                else setFailure({ key, error: problem });
            });
        return () => abort.abort();
    }, [ledgerId, ruleId, offset, key]);
    return (
        <section aria-label={t("已生成周期", "Generated occurrences")}>
            <h3>{t("已生成周期", "Generated occurrences")}</h3>
            <p className="help-text">
                {t(
                    "以下保留原生成内容；当前草稿可能已被人工修改或归档。重复任务不会覆盖它。",
                    "The original generated input is preserved below. The current draft may have been edited or archived; repeated jobs never overwrite it.",
                )}
            </p>
            <button type="button" disabled={locked} onClick={() => setRefresh((v) => v + 1)}>
                {t("刷新生成记录", "Refresh occurrences")}
            </button>
            {error != null && <p role="alert">{businessError(error, locale)}</p>}
            {!data && !error && <p role="status">{t("正在读取…", "Loading…")}</p>}
            {data?.length === 0 && (
                <p>{t("此页尚无生成记录。", "No generated occurrences on this page.")}</p>
            )}
            <ul className="master-records">
                {data?.map((row) => (
                    <li key={row.id} data-testid="recurring-instance">
                        <strong>
                            {row.scheduled_date} · {row.original_input.asset_id}
                        </strong>
                        <span>
                            {t("生成时规则版本", "Rule version at generation")} {row.rule_version}
                        </span>
                        <RecurringInput value={row.original_input} locale={locale} />
                        <button type="button" disabled={locked} onClick={() => onOpenDraft(row.id)}>
                            {t("查看当前草稿", "View current draft")}
                        </button>
                    </li>
                ))}
            </ul>
            <div className="form-actions">
                <button
                    type="button"
                    disabled={locked || !data || offset === 0}
                    onClick={() => setOffset((v) => v - 25)}
                >
                    {t("上一页记录", "Previous occurrences")}
                </button>
                <span>
                    {t("页", "Page")} {offset / 25 + 1}
                </span>
                <button
                    type="button"
                    disabled={locked || data?.length !== 25 || offset >= 100000}
                    onClick={() => setOffset((v) => v + 25)}
                >
                    {t("下一页记录", "Next occurrences")}
                </button>
            </div>
        </section>
    );
}
