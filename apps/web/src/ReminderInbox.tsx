import { useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import type { Locale } from "./i18n";
import { listInbox, markInboxRead } from "./reminder-inbox-api";
import type { InboxDelivery } from "./reminder-inbox-api";

const kindLabels = {
    annual: ["年审／年度报告", "Annual filing"],
    certificate: ["证件到期", "Certificate expiry"],
    tax: ["税务申报", "Tax filing"],
} as const;
const pageSize = 25;

function scoped(row: InboxDelivery, ledger: string) {
    return (
        row?.ledger_id === ledger &&
        row.channel === "inbox" &&
        typeof row.id === "string" &&
        typeof row.event_id === "string" &&
        Object.hasOwn(kindLabels, row.event_kind) &&
        ((row.version === 1 && row.read_at === null) ||
            (row.version === 2 && typeof row.read_at === "string"))
    );
}

export function ReminderInbox({
    ledger,
    locale,
    session,
    onUnauthorized,
    openEvent,
}: {
    ledger: string;
    locale: Locale;
    session: Session;
    onUnauthorized: () => void;
    openEvent: (id: string) => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [open, setOpen] = useState(false);
    const [unread, setUnread] = useState(false);
    const [offset, setOffset] = useState(0);
    const [refresh, setRefresh] = useState(0);
    const [result, setResult] = useState<{ key: string; rows: InboxDelivery[] | null } | null>(
        null,
    );
    const [pending, setPending] = useState<string | null>(null);
    const [readFailure, setReadFailure] = useState<string | null>(null);
    const key = JSON.stringify([ledger, unread, offset, refresh]);
    const latest = useRef({ key, onUnauthorized });
    latest.current = { key, onUnauthorized };
    const mutation = useRef<AbortController | null>(null);
    useEffect(() => () => mutation.current?.abort(), []);
    const page = result?.key === key ? result : null;

    useEffect(() => {
        if (!open) return;
        const controller = new AbortController();
        const active = () => !controller.signal.aborted && latest.current.key === key;
        void listInbox(ledger, unread, offset, controller.signal)
            .then((rows) => {
                if (!active()) return;
                if (
                    !Array.isArray(rows) ||
                    rows.some((row) => !scoped(row, ledger) || (unread && row.read_at !== null))
                )
                    throw new ApiError("server", 200, "invalid_response");
                setResult({ key, rows });
            })
            .catch((error: unknown) => {
                if (!active()) return;
                if (error instanceof ApiError && error.status === 401)
                    latest.current.onUnauthorized();
                else setResult({ key, rows: null });
            });
        return () => controller.abort();
    }, [open, ledger, unread, offset, key]);

    async function read(row: InboxDelivery) {
        if (mutation.current || row.read_at !== null) return;
        const controller = new AbortController();
        mutation.current = controller;
        setPending(row.id);
        setReadFailure(null);
        try {
            const receipt = await markInboxRead(
                session.csrf_token,
                ledger,
                row.id,
                controller.signal,
            );
            if (controller.signal.aborted) return;
            if (
                !scoped(receipt, ledger) ||
                receipt.id !== row.id ||
                receipt.event_id !== row.event_id ||
                receipt.version !== 2
            )
                throw new ApiError("server", 200, "invalid_response");
            setRefresh((value) => value + 1);
        } catch (error) {
            if (controller.signal.aborted) return;
            if (error instanceof ApiError && error.status === 401) latest.current.onUnauthorized();
            else setReadFailure(row.id);
        } finally {
            mutation.current = null;
            if (!controller.signal.aborted) setPending(null);
        }
    }

    const title = t("站内提醒", "Reminder inbox");
    return (
        <section aria-label={title} className="reminder-email-panel">
            <h3>{title}</h3>
            <button
                type="button"
                aria-expanded={open}
                disabled={Boolean(pending)}
                onClick={() => {
                    if (!open) setRefresh((value) => value + 1);
                    setOpen(!open);
                }}
            >
                {open ? t("收起站内提醒", "Hide inbox") : t("查看站内提醒", "View inbox")}
            </button>
            {open && (
                <>
                    <div className="form-actions">
                        <label>
                            <input
                                type="checkbox"
                                checked={unread}
                                disabled={Boolean(pending)}
                                onChange={(event) => {
                                    setUnread(event.target.checked);
                                    setOffset(0);
                                }}
                            />
                            {t("仅未读", "Unread only")}
                        </label>
                        <button
                            disabled={Boolean(pending)}
                            onClick={() => setRefresh((value) => value + 1)}
                        >
                            {t("刷新站内提醒", "Refresh inbox")}
                        </button>
                    </div>
                    {!page && <p role="status">{t("正在读取站内提醒…", "Loading inbox…")}</p>}
                    {page && !page.rows && (
                        <p role="alert">{t("读取站内提醒失败。", "Could not load the inbox.")}</p>
                    )}
                    {page?.rows?.length === 0 && (
                        <p>{t("此页暂无站内提醒。", "No inbox notices on this page.")}</p>
                    )}
                    <ol className="master-records">
                        {page?.rows?.map((row) => (
                            <li key={row.id} data-testid="reminder-inbox-delivery">
                                <strong>{row.title}</strong>
                                <span>{kindLabels[row.event_kind][locale === "zh" ? 0 : 1]}</span>
                                <span>
                                    {t("到期日", "Due date")} {row.due_date} ·{" "}
                                    {row.date_basis === "manual"
                                        ? t("人工日期", "Manual date")
                                        : t("规则日期", "Calculated date")}
                                </span>
                                <span>
                                    {t("计划时点", "Scheduled time")}{" "}
                                    <time dateTime={row.planned_at}>{row.planned_at}</time>
                                </span>
                                {row.read_at ? (
                                    <span>
                                        {t("已读时间", "Read at")}{" "}
                                        <time dateTime={row.read_at}>{row.read_at}</time>
                                    </span>
                                ) : (
                                    <button
                                        disabled={Boolean(pending)}
                                        onClick={() => void read(row)}
                                    >
                                        {pending === row.id
                                            ? t("正在标记…", "Marking as read…")
                                            : t("标为已读", "Mark as read")}
                                    </button>
                                )}
                                {readFailure === row.id && (
                                    <p role="alert">
                                        {t(
                                            "已读结果尚未确认，请重试标记。",
                                            "Read status is unconfirmed. Try marking it again.",
                                        )}
                                    </p>
                                )}
                                <button
                                    disabled={Boolean(pending)}
                                    onClick={() => openEvent(row.event_id)}
                                >
                                    {t("查看对应事项", "View source event")}
                                </button>
                            </li>
                        ))}
                    </ol>
                    {page?.rows && (offset > 0 || page.rows.length === pageSize) && (
                        <div className="form-actions">
                            <button
                                disabled={!offset || Boolean(pending)}
                                onClick={() => setOffset(offset - pageSize)}
                            >
                                {t("上一页", "Previous page")}
                            </button>
                            <button
                                disabled={
                                    page.rows.length !== pageSize ||
                                    offset >= 100000 ||
                                    Boolean(pending)
                                }
                                onClick={() => setOffset(offset + pageSize)}
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
