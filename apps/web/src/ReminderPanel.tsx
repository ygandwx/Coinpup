import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import type { Locale } from "./i18n";
import { getReminder, listReminders, listReminderRules } from "./reminder-api";
import type { Reminder, ReminderRule, ReminderTransition } from "./reminder-api";
import type { PendingReminderController } from "./pending-reminder";
import { scopedReminder } from "./reminder-intent";
import {
    createReminderBody,
    editReminderBody,
    manualReminderBody,
    recalculateReminderBody,
    reminderFields,
} from "./reminder-fields";
import type { ReminderFields } from "./reminder-fields";
import { ReminderForm } from "./ReminderForm";
import type { ReminderEditorMode } from "./ReminderForm";
import { ReminderEvidence } from "./ReminderEvidence";
import { ReminderHistory } from "./ReminderHistory";
import { ReminderOriginal } from "./ReminderOriginal";
import { reminderError } from "./reminder-errors";
import { actionLabel, statusLabel } from "./reminder-labels";
import "./reminder.css";

type Editor = {
    id: string;
    row: Reminder | null;
    mode: ReminderEditorMode | null;
    fields: ReminderFields | null;
};
export function ReminderPanel({
    ledgerId,
    session,
    locale,
    controller,
    archived,
    dataLoading,
    onEditing,
    onUnauthorized,
}: {
    ledgerId: string;
    session: Session;
    locale: Locale;
    controller: PendingReminderController;
    archived: boolean;
    dataLoading: boolean;
    onEditing: (editing: boolean) => void;
    onUnauthorized: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const ownerId = String(session.user.id);
    const kindLabel = (kind: Reminder["event_kind"]) =>
        kind === "annual"
            ? t("年审／年度报告", "Annual filing")
            : kind === "tax"
              ? t("税务申报", "Tax filing")
              : t("证件到期", "Certificate expiry");
    const pending = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
    const plan = pending.plan?.ledgerId === ledgerId ? pending.plan : null;
    const [editor, setEditor] = useState<Editor | null>(() =>
        plan ? { id: plan.id, row: null, mode: null, fields: null } : null,
    );
    const [offset, setOffset] = useState(0),
        [refresh, setRefresh] = useState(0);
    const [result, setResult] = useState<{
        key: string;
        rows: Reminder[];
        rules: ReminderRule[];
    } | null>(null);
    const [failure, setFailure] = useState<{ key: string; error: unknown } | null>(null);
    const [localError, setLocalError] = useState<unknown>(null),
        [reading, setReading] = useState(false);
    const key = JSON.stringify([ownerId, ledgerId, offset, refresh]);
    const current = useRef({ key, ownerId, ledgerId, onEditing, onUnauthorized });
    current.current = { key, ownerId, ledgerId, onEditing, onUnauthorized };
    const data = result?.key === key ? result : null,
        readError = failure?.key === key ? failure.error : null;
    const unresolved = !["idle", "confirmed", "conflict", "rejected"].includes(pending.status);
    const confirmed = !!plan && pending.status === "confirmed",
        conflict = !!plan && pending.status === "conflict";
    const detailAbort = useRef<AbortController | null>(null);
    useEffect(() => () => detailAbort.current?.abort(), []);
    useEffect(() => {
        current.current.onEditing(!!editor);
        return () => current.current.onEditing(false);
    }, [editor]);
    useEffect(() => {
        if (pending.status === "auth-required") current.current.onUnauthorized();
    }, [pending.status]);
    useEffect(() => {
        const abort = new AbortController();
        const active = () => !abort.signal.aborted && current.current.key === key;
        Promise.all([
            listReminders(ledgerId, offset, abort.signal),
            listReminderRules(ledgerId, abort.signal),
        ])
            .then(([rows, rules]) => {
                if (!active()) return;
                if (
                    !Array.isArray(rows) ||
                    rows.some(
                        (row) =>
                            typeof row?.id !== "string" ||
                            !row.id ||
                            !scopedReminder(row, {
                                ownerId,
                                ledgerId,
                                id: row?.id,
                                action: "create",
                                bodyJson: "{}",
                            }),
                    ) ||
                    !Array.isArray(rules)
                )
                    throw new ApiError("server", 200, "invalid_response");
                setResult({ key, rows, rules });
            })
            .catch((error) => {
                if (!active()) return;
                if (error instanceof ApiError && error.status === 401)
                    current.current.onUnauthorized();
                else setFailure({ key, error });
            });
        return () => abort.abort();
    }, [ownerId, ledgerId, offset, key]);
    function fail(error: unknown) {
        if (error instanceof ApiError && error.status === 401) current.current.onUnauthorized();
        else setLocalError(error);
    }
    function finish() {
        if (!controller.dismiss()) return;
        detailAbort.current?.abort();
        setReading(false);
        setEditor(null);
        setLocalError(null);
        setRefresh((value) => value + 1);
    }
    async function open(id: string) {
        if (reading || !controller.dismiss()) return;
        detailAbort.current?.abort();
        const abort = new AbortController();
        detailAbort.current = abort;
        const active = () =>
            !abort.signal.aborted &&
            current.current.ownerId === ownerId &&
            current.current.ledgerId === ledgerId;
        setReading(true);
        setLocalError(null);
        setEditor({ id, row: null, mode: null, fields: null });
        try {
            const row = await getReminder(ledgerId, id, abort.signal);
            if (!active()) return;
            if (!scopedReminder(row, { ownerId, ledgerId, id, action: "create", bodyJson: "{}" }))
                throw new ApiError("server", 200, "invalid_response");
            setEditor({ id, row, mode: null, fields: null });
        } catch (error) {
            if (active()) fail(error);
        } finally {
            if (active()) setReading(false);
        }
    }
    const disabled = archived || dataLoading || reading || unresolved;
    function begin(mode: ReminderEditorMode) {
        if (disabled || !controller.dismiss()) return;
        setLocalError(null);
        if (mode === "create")
            setEditor({ id: crypto.randomUUID(), row: null, mode, fields: reminderFields() });
        else if (editor?.row) setEditor({ ...editor, mode, fields: reminderFields(editor.row) });
    }
    async function save(fields: ReminderFields) {
        if (
            !editor?.mode ||
            disabled ||
            !data ||
            !["idle", "rejected"].includes(controller.getSnapshot().status)
        )
            return;
        setLocalError(null);
        setEditor({ ...editor, fields });
        try {
            const mode = editor.mode,
                version = editor.row?.version ?? 0;
            const body =
                mode === "create"
                    ? createReminderBody(editor.id, fields, data.rules)
                    : mode === "save"
                      ? editReminderBody(version, fields)
                      : mode === "recalculate"
                        ? recalculateReminderBody(version, fields, data.rules)
                        : manualReminderBody(version, fields, mode === "clear");
            await controller.start(
                session,
                ledgerId,
                editor.id,
                mode === "clear" ? "manual" : mode,
                body,
            );
        } catch (error) {
            fail(error);
        }
    }
    async function transition(action: ReminderTransition["action"]) {
        if (
            disabled ||
            !editor?.row ||
            !["idle", "rejected"].includes(controller.getSnapshot().status)
        )
            return;
        setLocalError(null);
        try {
            await controller.start(session, ledgerId, editor.id, "transition", {
                expected_version: editor.row.version,
                action,
            });
        } catch (error) {
            fail(error);
        }
    }
    const displayed = plan && pending.record ? pending.record : editor?.row;
    return (
        <section className="panel reminder-panel" aria-label={t("提醒事项", "Reminder events")}>
            <h2>{t("提醒事项", "Reminder events")}</h2>
            <p className="help-text">
                {t(
                    "日期与历史可在此管理；站内和邮件投递尚未启用。刷新或关闭页面会丢失未保存输入和内存请求。",
                    "Manage dates and history here; inbox and email delivery are not enabled yet. Reloading or closing loses unsaved input and in-memory requests.",
                )}
            </p>
            {archived && (
                <p role="status">
                    {t("主体已归档，仅可查看。", "This entity is archived; events are read-only.")}
                </p>
            )}
            {readError != null && <p role="alert">{reminderError(readError, locale)}</p>}
            {editor ? (
                <>
                    {reading && <p role="status">{t("正在读取事项…", "Loading event…")}</p>}
                    {displayed && (
                        <>
                            <h3>{displayed.title}</h3>
                            <p>
                                {t("版本", "Version")} {displayed.version} ·{" "}
                                {kindLabel(displayed.event_kind)} ·{" "}
                                {displayed.completed
                                    ? t("已完成", "Completed")
                                    : t("未完成", "Open")}{" "}
                                ·{" "}
                                {displayed.archived
                                    ? t("已归档", "Archived")
                                    : t("未归档", "Active")}
                            </p>
                            {displayed.notes && <p className="reminder-notes">{displayed.notes}</p>}
                            <ReminderEvidence value={displayed} locale={locale} />
                        </>
                    )}
                    {confirmed ? (
                        <>
                            <p role="status">
                                {t("事项已保存并核对。", "Event saved and verified.")}
                            </p>
                            <button onClick={() => void open(editor.id)}>
                                {t("查看已保存事项", "View saved event")}
                            </button>
                            <button onClick={finish}>{t("返回事项列表", "Back to events")}</button>
                        </>
                    ) : (
                        <>
                            {plan && (unresolved || conflict) && (
                                <ReminderOriginal plan={plan} locale={locale} />
                            )}
                            {editor.mode && editor.fields && !unresolved && !conflict && (
                                <ReminderForm
                                    key={`${editor.id}:${editor.row?.version ?? 0}:${editor.mode}`}
                                    initial={editor.fields}
                                    mode={editor.mode}
                                    rules={data?.rules ?? []}
                                    locale={locale}
                                    locked={disabled || !data}
                                    canFinish={!unresolved}
                                    onSave={save}
                                    onCancel={() => {
                                        setEditor({ ...editor, mode: null, fields: null });
                                        if (!editor.row) finish();
                                    }}
                                />
                            )}
                            {editor.row && !editor.mode && !unresolved && !conflict && (
                                <div className="form-actions">
                                    {!editor.row.archived && (
                                        <>
                                            <button
                                                disabled={disabled}
                                                onClick={() => begin("save")}
                                            >
                                                {t("编辑资料", "Edit details")}
                                            </button>
                                            <button
                                                disabled={disabled || !data}
                                                onClick={() => begin("recalculate")}
                                            >
                                                {t("明确重算", "Recalculate explicitly")}
                                            </button>
                                            <button
                                                disabled={disabled}
                                                onClick={() => begin("manual")}
                                            >
                                                {t("设置人工日期", "Set manual date")}
                                            </button>
                                            {editor.row.manual_due_date && (
                                                <button
                                                    disabled={disabled}
                                                    onClick={() => begin("clear")}
                                                >
                                                    {t("清除人工日期", "Clear manual date")}
                                                </button>
                                            )}
                                            <button
                                                disabled={disabled}
                                                onClick={() =>
                                                    void transition(
                                                        editor.row!.completed
                                                            ? "reopen"
                                                            : "complete",
                                                    )
                                                }
                                            >
                                                {editor.row.completed
                                                    ? t("重开事项", "Reopen event")
                                                    : t("标记完成", "Mark completed")}
                                            </button>
                                        </>
                                    )}
                                    <button
                                        disabled={disabled}
                                        onClick={() =>
                                            void transition(
                                                editor.row!.archived ? "restore" : "archive",
                                            )
                                        }
                                    >
                                        {editor.row.archived
                                            ? t("恢复事项", "Restore event")
                                            : t("归档事项", "Archive event")}
                                    </button>
                                    <button disabled={reading} onClick={finish}>
                                        {t("返回事项列表", "Back to events")}
                                    </button>
                                </div>
                            )}
                            {pending.status === "submitting" && (
                                <p role="status">{t("正在保存…", "Saving…")}</p>
                            )}
                            {plan && pending.status === "unknown" && (
                                <div role="alert">
                                    <p>
                                        {t(
                                            "结果尚未确认，请重试原请求，不要另建事项或新操作。",
                                            "The result is unconfirmed. Retry the original request instead of creating another event or action.",
                                        )}
                                    </p>
                                    <button
                                        onClick={() => void controller.retry(session).catch(fail)}
                                    >
                                        {t("重试原事项请求", "Retry original event request")}
                                    </button>
                                </div>
                            )}
                            {conflict && (
                                <div role="alert">
                                    <p>
                                        {t(
                                            "事项版本已变化。当前记录不能证明原请求成功；请核对历史。重新载入会替换当前输入。",
                                            "The event version changed. Current data does not prove the original request succeeded; review history. Reload replaces current input.",
                                        )}
                                    </p>
                                    <button onClick={() => void open(editor.id)}>
                                        {t("重新载入当前事项", "Reload current event")}
                                    </button>
                                    <button onClick={finish}>
                                        {t("返回事项列表", "Back to events")}
                                    </button>
                                </div>
                            )}
                            {pending.status === "rejected" && pending.error && (
                                <p role="alert">{reminderError(pending.error, locale)}</p>
                            )}
                            {!editor.row && !editor.mode && !unresolved && !conflict && (
                                <div className="form-actions">
                                    <button disabled={reading} onClick={() => void open(editor.id)}>
                                        {t("重试读取事项", "Retry loading event")}
                                    </button>
                                    <button onClick={finish}>
                                        {t("返回事项列表", "Back to events")}
                                    </button>
                                </div>
                            )}
                        </>
                    )}
                    {displayed && (
                        <ReminderHistory
                            key={`${ownerId}:${ledgerId}:${displayed.id}:${displayed.version}`}
                            ownerId={ownerId}
                            ledgerId={ledgerId}
                            eventId={displayed.id}
                            locale={locale}
                            locked={unresolved}
                            onUnauthorized={onUnauthorized}
                        />
                    )}
                    {localError != null && <p role="alert">{reminderError(localError, locale)}</p>}
                </>
            ) : (
                <>
                    <div className="form-actions">
                        <button disabled={disabled || !data} onClick={() => begin("create")}>
                            {t("新增提醒事项", "New reminder event")}
                        </button>
                        <button
                            disabled={unresolved}
                            onClick={() => setRefresh((value) => value + 1)}
                        >
                            {t("刷新事项", "Refresh events")}
                        </button>
                    </div>
                    {!data && readError == null && (
                        <p role="status">{t("正在读取事项…", "Loading events…")}</p>
                    )}
                    {data?.rows.length === 0 && (
                        <p>
                            {t("此账本尚无提醒事项。", "This ledger has no reminder events yet.")}
                        </p>
                    )}
                    {data?.rows.map((row) => (
                        <article key={row.id} className="reminder-list-item">
                            <h3>{row.title}</h3>
                            <p>
                                {row.effective_date ?? t("日期未确定", "Date not determined")} ·{" "}
                                {statusLabel(row.evaluation_status, locale)} ·{" "}
                                {kindLabel(row.event_kind)} ·{" "}
                                {row.completed ? t("已完成", "Completed") : t("未完成", "Open")} ·{" "}
                                {row.archived ? t("已归档", "Archived") : t("未归档", "Active")}
                            </p>
                            <p className="help-text">
                                {actionLabel(row.last_action, locale)} · {t("版本", "Version")}{" "}
                                {row.version}
                            </p>
                            <button
                                disabled={reading || unresolved}
                                onClick={() => void open(row.id)}
                            >
                                {t("查看事项", "View event")}: {row.title}
                            </button>
                        </article>
                    ))}
                    <div className="form-actions">
                        <button
                            disabled={!data || offset === 0}
                            onClick={() => setOffset((value) => Math.max(0, value - 25))}
                        >
                            {t("上一页", "Previous page")}
                        </button>
                        <button
                            disabled={!data || data.rows.length < 25 || offset >= 100000}
                            onClick={() => setOffset((value) => value + 25)}
                        >
                            {t("下一页", "Next page")}
                        </button>
                    </div>
                </>
            )}
        </section>
    );
}
