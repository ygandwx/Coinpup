import { useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import type { Locale } from "./i18n";
import { listReminderRevisions } from "./reminder-api";
import type { ReminderRevision } from "./reminder-api";
import { ReminderEvidence } from "./ReminderEvidence";
import { reminderHistoryRows } from "./reminder-history";
import { actionLabel } from "./reminder-labels";

export function ReminderHistory({
    ownerId,
    ledgerId,
    eventId,
    locale,
    locked,
    onUnauthorized,
}: {
    ownerId: string;
    ledgerId: string;
    eventId: string;
    locale: Locale;
    locked: boolean;
    onUnauthorized: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [offset, setOffset] = useState(0),
        [refresh, setRefresh] = useState(0);
    const [result, setResult] = useState<{ key: string; rows: ReminderRevision[] } | null>(null);
    const [failure, setFailure] = useState<{ key: string; error: unknown } | null>(null);
    const key = JSON.stringify([ownerId, ledgerId, eventId, offset, refresh]);
    const current = useRef({ key, onUnauthorized });
    current.current = { key, onUnauthorized };
    const data = result?.key === key ? result.rows : null,
        error = failure?.key === key ? failure.error : null;
    useEffect(() => {
        const abort = new AbortController();
        const active = () => !abort.signal.aborted && current.current.key === key;
        listReminderRevisions(ledgerId, eventId, offset, abort.signal)
            .then((rows) => {
                if (!active()) return;
                if (!reminderHistoryRows(rows, ownerId, ledgerId, eventId, offset))
                    throw new ApiError("server", 200, "invalid_response");
                setResult({ key, rows });
            })
            .catch((problem) => {
                if (!active()) return;
                if (problem instanceof ApiError && problem.status === 401)
                    current.current.onUnauthorized();
                else setFailure({ key, error: problem });
            });
        return () => abort.abort();
    }, [ownerId, ledgerId, eventId, offset, key]);
    return (
        <section aria-label={t("事项修订历史", "Event revision history")}>
            <h3>{t("事项修订历史", "Event revision history")}</h3>
            <p className="help-text">
                {t(
                    "历史保留当时的推算依据和操作；不会按最新规则重新计算。",
                    "History preserves the evidence and action at that time. It is not recalculated using the latest rules.",
                )}
            </p>
            <button
                type="button"
                disabled={locked}
                onClick={() => setRefresh((value) => value + 1)}
            >
                {t("刷新历史", "Refresh history")}
            </button>
            {error != null && (
                <p role="alert">
                    {t("未能读取历史，请重试。", "History could not be loaded. Please retry.")}
                </p>
            )}
            {!data && !error && <p role="status">{t("正在读取…", "Loading…")}</p>}
            {data?.length === 0 && <p>{t("此页没有修订记录。", "No revisions on this page.")}</p>}
            <ol className="master-records">
                {data?.map((row) => (
                    <li key={row.id} data-testid="reminder-revision">
                        <strong>
                            {t("版本", "Version")} {row.version} ·{" "}
                            {actionLabel(String(row.snapshot.last_action), locale)}
                        </strong>
                        <span>
                            {typeof row.snapshot.title === "string" ? row.snapshot.title : ""}
                        </span>
                        <time dateTime={row.created_at}>
                            {t("操作时间（本地）", "Action time (local)")}:{" "}
                            {new Date(row.created_at).toLocaleString(
                                locale === "zh" ? "zh-CN" : "en-GB",
                            )}
                        </time>
                        {typeof row.snapshot.notes === "string" && <p>{row.snapshot.notes}</p>}
                        <p>
                            {row.snapshot.completed
                                ? t("已完成", "Completed")
                                : t("未完成", "Open")}{" "}
                            ·{" "}
                            {row.snapshot.archived
                                ? t("已归档", "Archived")
                                : t("未归档", "Active")}
                        </p>
                        {typeof row.snapshot.last_reason === "string" && (
                            <p>
                                {t("操作理由", "Action reason")}: {row.snapshot.last_reason}
                            </p>
                        )}
                        <ReminderEvidence value={row.snapshot} locale={locale} />
                    </li>
                ))}
            </ol>
            <div className="form-actions">
                <button
                    type="button"
                    disabled={locked || !data || offset === 0}
                    onClick={() => setOffset((value) => value - 25)}
                >
                    {t("上一页历史", "Previous history")}
                </button>
                <span>
                    {t("页", "Page")} {offset / 25 + 1}
                </span>
                <button
                    type="button"
                    disabled={locked || data?.length !== 25 || offset >= 100000}
                    onClick={() => setOffset((value) => value + 25)}
                >
                    {t("下一页历史", "Next history")}
                </button>
            </div>
        </section>
    );
}
