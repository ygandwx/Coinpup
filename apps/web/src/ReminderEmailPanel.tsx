import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { FormEvent } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import type { Locale } from "./i18n";
import type { PendingReminderEmail } from "./pending-reminder-email";
import { listEmailDeliveries, readDelivery } from "./reminder-email-api";
import type { DeliverySettings, DeliveryView, EmailDelivery } from "./reminder-email-api";
import { reminderError } from "./reminder-errors";
import { ReminderEmailDecision } from "./ReminderEmailDecision";

const labels = {
    sending: ["正在投递", "Sending"],
    retry_wait: ["等待重试", "Waiting to retry"],
    failed: ["明确失败", "Failed"],
    unknown: ["结果未知", "Outcome unknown"],
    accepted: ["SMTP 已接受", "Accepted by SMTP"],
    cancelled: ["已取消", "Cancelled"],
    verified_received: ["已核实收到", "Receipt verified"],
    stopped: ["已停止", "Stopped"],
} as const;
const reasons = {
    disabled: ["投递已停用", "Delivery disabled"],
    unplanned: ["计划待更新", "Schedule needs updating"],
    missing_date: ["缺少有效日期", "No effective due date"],
    completed: ["事项已完成", "Event completed"],
    archived_event: ["事项已归档", "Event archived"],
    archived_entity: ["主体已归档", "Entity archived"],
    timezone_unavailable: ["时区不可用", "Time zone unavailable"],
    date_out_of_range: ["日期越界", "Date out of range"],
    local_date_unavailable: ["当地日期不可用", "Local date unavailable"],
} as const;
const slot = (minute: number) =>
    [Math.floor(minute / 60), minute % 60].map((part) => String(part).padStart(2, "0")).join(":");
const pageSize = 25;
type Copy = (zh: string, en: string) => string;

function Settings({
    view,
    locale,
    t,
    blocked,
    save,
}: {
    view: DeliveryView;
    locale: Locale;
    t: Copy;
    blocked: boolean;
    save: (body: DeliverySettings) => void;
}) {
    const c = view.config;
    const [enabled, setEnabled] = useState(Boolean(c?.enabled && c.email_enabled));
    const [inbox, setInbox] = useState(Boolean(c?.enabled && c.inbox_enabled));
    function submit(event: FormEvent<HTMLFormElement>) {
        event.preventDefault();
        if (blocked) return;
        const form = new FormData(event.currentTarget);
        const value = (name: string) => String(form.get(name) ?? "");
        const [hour, minute] = value("time").split(":").map(Number);
        save({
            expected_event_version: view.event_version,
            expected_config_version: c?.version ?? 0,
            enabled: enabled || inbox,
            inbox_enabled: inbox,
            email_enabled: enabled,
            email_recipient: value("recipient").trim() || null,
            lead_days: Number(value("lead")),
            timezone: value("zone"),
            minute: hour * 60 + minute,
            locale: value("locale") === "zh" ? "zh" : "en",
        });
    }
    return (
        <form aria-label={t("邮件设置", "Email settings")} onSubmit={submit}>
            <fieldset disabled={blocked} className="reminder-email-fields">
                <legend>{t("投递设置", "Delivery settings")}</legend>
                <label>
                    <input
                        type="checkbox"
                        checked={inbox}
                        onChange={(event) => setInbox(event.target.checked)}
                    />
                    {t("启用站内提醒", "Enable inbox reminders")}
                </label>
                <label>
                    <input
                        type="checkbox"
                        checked={enabled}
                        onChange={(event) => setEnabled(event.target.checked)}
                    />
                    {t("启用邮件提醒", "Enable email reminders")}
                </label>
                <div className="form-grid">
                    <div className="field">
                        <label htmlFor="email-recipient">{t("接收地址", "Email recipient")}</label>
                        <input
                            id="email-recipient"
                            name="recipient"
                            type="email"
                            required={enabled}
                            maxLength={320}
                            defaultValue={c?.email_recipient ?? ""}
                        />
                    </div>
                    <div className="field">
                        <label htmlFor="email-lead">{t("到期前天数", "Days before due")}</label>
                        <input
                            id="email-lead"
                            name="lead"
                            type="number"
                            min="0"
                            max="3660"
                            step="1"
                            required
                            defaultValue={c?.lead_days ?? 0}
                        />
                    </div>
                    <div className="field">
                        <label htmlFor="email-zone">{t("时区", "Time zone")}</label>
                        <input
                            id="email-zone"
                            name="zone"
                            required
                            maxLength={64}
                            defaultValue={c?.timezone ?? "UTC"}
                        />
                    </div>
                    <div className="field">
                        <label htmlFor="email-time">{t("当地时间", "Local time")}</label>
                        <input
                            id="email-time"
                            name="time"
                            type="time"
                            required
                            defaultValue={slot(c?.minute ?? 540)}
                        />
                    </div>
                    <div className="field">
                        <label htmlFor="email-lang">{t("邮件语言", "Email language")}</label>
                        <select id="email-lang" name="locale" defaultValue={c?.locale ?? locale}>
                            <option value="zh">中文</option>
                            <option value="en">English</option>
                        </select>
                    </div>
                </div>
                <button type="submit">{t("保存投递设置", "Save delivery settings")}</button>
            </fieldset>
        </form>
    );
}

export function ReminderEmailPanel({
    ledger,
    event,
    session,
    locale,
    controller,
    readOnly,
    onUnauthorized,
}: {
    ledger: string;
    event: string;
    session: Session;
    locale: Locale;
    controller: PendingReminderEmail;
    readOnly: boolean;
    onUnauthorized: () => void;
}) {
    const t: Copy = (zh, en) => (locale === "zh" ? zh : en);
    const pending = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
    const scoped = pending.plan?.ledgerId === ledger && pending.plan.id === event;
    const locked = ["submitting", "unknown", "auth-required"].includes(pending.status);
    const [open, setOpen] = useState(Boolean(scoped));
    const [offset, setOffset] = useState(0);
    const [refresh, setRefresh] = useState(0);
    const key = JSON.stringify([ledger, event, offset, refresh]);
    const [read, setRead] = useState<{
        key: string;
        settings: DeliveryView;
        rows: EmailDelivery[];
    } | null>(null);
    const [failure, setFailure] = useState<{ key: string; error: unknown } | null>(null);
    const latest = useRef({ key, onUnauthorized });
    latest.current = { key, onUnauthorized };
    const data = read?.key === key ? read : null;
    const error = failure?.key === key ? failure.error : null;
    const deciding = pending.plan?.kind === "decision";
    const effectiveSettings =
        scoped && pending.status === "confirmed" && pending.record && "config" in pending.record
            ? pending.record
            : read?.settings;
    useEffect(() => {
        if (!open) return;
        const abort = new AbortController();
        const active = () => !abort.signal.aborted && latest.current.key === key;
        Promise.all([
            readDelivery(ledger, event, abort.signal),
            listEmailDeliveries(ledger, event, offset, pageSize, abort.signal),
        ])
            .then(([settings, rows]) => {
                if (!active()) return;
                if (
                    settings?.ledger_id !== ledger ||
                    settings.event_id !== event ||
                    !Array.isArray(rows) ||
                    rows.some(
                        (row) =>
                            row?.ledger_id !== ledger ||
                            row.event_id !== event ||
                            row.channel !== "email" ||
                            !Object.hasOwn(labels, row.state),
                    )
                ) {
                    throw new ApiError("server", 200, "invalid_response");
                }
                setRead({ key, settings, rows });
            })
            .catch((issue: unknown) => {
                if (!active()) return;
                if (issue instanceof ApiError && issue.status === 401)
                    latest.current.onUnauthorized();
                else setFailure({ key, error: issue });
            });
        return () => abort.abort();
    }, [open, key, ledger, event, offset]);
    useEffect(() => {
        if (pending.status === "auth-required") latest.current.onUnauthorized();
        if (scoped && pending.status === "confirmed") setRefresh((value) => value + 1);
    }, [pending.status, scoped]);
    function reload() {
        if (scoped) controller.dismiss();
        setRefresh((value) => value + 1);
    }
    function fail(error: unknown) {
        if (error instanceof ApiError && error.status === 401) latest.current.onUnauthorized();
        else setFailure({ key, error });
    }
    const title = t("提醒投递", "Reminder delivery");
    const config = effectiveSettings?.config;
    return (
        <section className="reminder-email-panel" aria-label={title}>
            <h3>{title}</h3>
            <button
                type="button"
                aria-expanded={open}
                disabled={locked}
                onClick={() => setOpen(!open)}
            >
                {open
                    ? t("收起投递设置", "Hide delivery settings")
                    : t("查看投递设置", "View delivery settings")}
            </button>
            {open && (
                <>
                    {error != null && <p role="alert">{reminderError(error, locale)}</p>}
                    {!data && !error && (
                        <p role="status">{t("正在读取邮件设置…", "Loading email settings…")}</p>
                    )}
                    {scoped && pending.status === "unknown" && (
                        <div role="alert">
                            <p>
                                {deciding
                                    ? t(
                                          "处理结果尚未确认。",
                                          "The delivery decision is unconfirmed.",
                                      )
                                    : t(
                                          "邮件设置保存结果尚未确认。",
                                          "The email settings save is unconfirmed.",
                                      )}
                            </p>
                            <button onClick={() => void controller.retry(session).catch(fail)}>
                                {deciding
                                    ? t("重试原处理请求", "Retry original outcome request")
                                    : t("重试原设置请求", "Retry original settings request")}
                            </button>
                        </div>
                    )}
                    {scoped && pending.status === "conflict" && (
                        <div role="alert">
                            <p>
                                {t(
                                    "事项或投递已变化，请核对当前记录。",
                                    "The event or delivery changed. Review the current record.",
                                )}
                            </p>
                            <button onClick={reload}>
                                {t(
                                    "载入当前邮件设置和历史",
                                    "Load current email settings and history",
                                )}
                            </button>
                        </div>
                    )}
                    {scoped && pending.status === "rejected" && pending.error && (
                        <p role="alert">{reminderError(pending.error, locale)}</p>
                    )}
                    {scoped && pending.status === "confirmed" && (
                        <p role="status">
                            {deciding
                                ? t("投递处理已保存。", "Delivery decision saved.")
                                : t("投递设置已保存。", "Delivery settings saved.")}
                        </p>
                    )}
                    {effectiveSettings && (
                        <>
                            {config?.saved_plan && (
                                <p>
                                    {t("计划时点", "Scheduled time")}{" "}
                                    <time dateTime={config.saved_plan.scheduled_at}>
                                        {config.saved_plan.scheduled_at}
                                    </time>
                                </p>
                            )}
                            {config?.preview_blocked_reason && (
                                <p>
                                    {
                                        reasons[config.preview_blocked_reason][
                                            locale === "zh" ? 0 : 1
                                        ]
                                    }
                                </p>
                            )}
                            {config?.needs_refresh && (
                                <p>
                                    {t(
                                        "事项或设置已变化，计划待更新。",
                                        "The event or settings changed. The schedule needs updating.",
                                    )}
                                </p>
                            )}
                            <Settings
                                key={JSON.stringify([
                                    effectiveSettings.event_version,
                                    config?.version,
                                ])}
                                view={effectiveSettings}
                                locale={locale}
                                t={t}
                                blocked={
                                    readOnly || locked || (scoped && pending.status === "conflict")
                                }
                                save={(body) =>
                                    void controller.start(session, ledger, event, body).catch(fail)
                                }
                            />
                        </>
                    )}
                    <h4>{t("邮件投递历史", "Email delivery history")}</h4>
                    <button disabled={locked} onClick={reload}>
                        {t("刷新邮件历史", "Refresh email history")}
                    </button>
                    {data?.rows.length === 0 && (
                        <p>{t("没有邮件投递记录。", "No email deliveries yet.")}</p>
                    )}
                    {data && data.rows.length > 0 && (
                        <ol className="master-records">
                            {data.rows.map((row) => (
                                <li key={row.id}>
                                    <strong>{labels[row.state][locale === "zh" ? 0 : 1]}</strong>
                                    {" · "}
                                    <time dateTime={row.planned_at}>{row.planned_at}</time>
                                    {" · "}
                                    {t("尝试次数", "Attempts")} {row.attempt_no}
                                    {row.smtp_code !== null && <span>SMTP {row.smtp_code}</span>}
                                    {row.state === "accepted" && (
                                        <p>
                                            {t(
                                                "SMTP 接受仅表示邮件服务器确认接收。",
                                                "SMTP acceptance records acknowledgment by the mail server.",
                                            )}
                                        </p>
                                    )}
                                    {row.retry_at && (
                                        <p>
                                            {t("计划重试", "Retry scheduled")}{" "}
                                            <time dateTime={row.retry_at}>{row.retry_at}</time>
                                        </p>
                                    )}
                                    <ReminderEmailDecision
                                        row={row}
                                        locale={locale}
                                        disabled={
                                            readOnly ||
                                            locked ||
                                            (scoped && pending.status === "conflict")
                                        }
                                        decide={(body) =>
                                            void controller
                                                .decide(session, ledger, event, row.id, body)
                                                .catch(fail)
                                        }
                                    />
                                </li>
                            ))}
                        </ol>
                    )}
                    {data && (offset > 0 || data.rows.length === pageSize) && (
                        <div className="form-actions">
                            <button
                                disabled={locked || offset === 0}
                                onClick={() => setOffset(offset - pageSize)}
                            >
                                {t("上一页", "Previous page")}
                            </button>
                            <button
                                disabled={locked || data.rows.length < pageSize || offset >= 100000}
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
