import { useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import type { Locale } from "./i18n";
import { listBusinessDrafts } from "./business-draft-api";
import type { BusinessDraftSummary } from "./business-draft-api";
import { businessError } from "./business-errors";
import { formatAmount } from "./money";
import { usableRecurringSource } from "./recurring-fields";

export function RecurringSourcePicker({
    ledgerId,
    locale,
    selected,
    onSelect,
    onUnauthorized,
}: {
    ledgerId: string;
    locale: Locale;
    selected: BusinessDraftSummary | null;
    onSelect: (source: BusinessDraftSummary) => void;
    onUnauthorized: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [offset, setOffset] = useState(0),
        [refresh, setRefresh] = useState(0);
    const [result, setResult] = useState<{ key: string; rows: BusinessDraftSummary[] } | null>(
        null,
    );
    const [failure, setFailure] = useState<{ key: string; error: unknown } | null>(null);
    const callbacks = useRef({ onUnauthorized });
    callbacks.current = { onUnauthorized };
    const key = JSON.stringify([ledgerId, offset, refresh]);
    const data = result?.key === key ? result.rows : null;
    const error = failure?.key === key ? failure.error : null;
    useEffect(() => {
        const abort = new AbortController();
        listBusinessDrafts(ledgerId, offset, abort.signal)
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
    }, [ledgerId, offset, key]);
    const rows = data?.filter(usableRecurringSource) ?? [];
    return (
        <section aria-label={t("选择模板草稿", "Choose template draft")}>
            <p className="help-text">
                {t(
                    "选择未归档的销售Invoice草稿；保存时会核对版本。",
                    "Choose an active sales invoice draft; its version is checked when saving.",
                )}
            </p>
            <button type="button" onClick={() => setRefresh((v) => v + 1)}>
                {t("刷新模板列表", "Refresh template list")}
            </button>
            {error != null && <p role="alert">{businessError(error, locale)}</p>}
            {!data && !error && <p role="status">{t("正在读取…", "Loading…")}</p>}
            {data && !rows.length && (
                <p>
                    {t(
                        "此页没有可用Invoice草稿，请翻页或先创建草稿。",
                        "No available invoice drafts on this page. Browse another page or create a draft first.",
                    )}
                </p>
            )}
            <ul className="master-records">
                {rows.map((row) => (
                    <li key={row.id}>
                        <strong>{String(row.party_snapshot.name ?? "—")}</strong>
                        <span>
                            {row.issue_date} · {formatAmount(row.total_amount, locale)}{" "}
                            {row.asset_id} · {t("版本", "Version")} {row.version}
                        </span>
                        <button
                            type="button"
                            data-testid="recurring-source"
                            aria-pressed={
                                selected?.id === row.id && selected.version === row.version
                            }
                            onClick={() => onSelect(row)}
                        >
                            {t("选择此草稿", "Choose this draft")}
                        </button>
                    </li>
                ))}
            </ul>
            <div className="form-actions">
                <button
                    type="button"
                    disabled={!data || offset === 0}
                    onClick={() => setOffset((v) => v - 25)}
                >
                    {t("上一页模板", "Previous templates")}
                </button>
                <span>
                    {t("页", "Page")} {offset / 25 + 1}
                </span>
                <button
                    type="button"
                    disabled={data?.length !== 25 || offset >= 100000}
                    onClick={() => setOffset((v) => v + 25)}
                >
                    {t("下一页模板", "Next templates")}
                </button>
            </div>
        </section>
    );
}
