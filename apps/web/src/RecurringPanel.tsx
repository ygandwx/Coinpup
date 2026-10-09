import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import type { Locale } from "./i18n";
import type { BusinessDraftSummary } from "./business-draft-api";
import { getRecurringRule, listRecurringRules } from "./recurring-api";
import type { RecurringRule } from "./recurring-api";
import type { PendingRecurringController } from "./pending-recurring";
import { businessError } from "./business-errors";
import { localDraftDate } from "./draft-fields";
import { recurringCreate, recurringFields } from "./recurring-fields";
import type { RecurringFields } from "./recurring-fields";
import { RecurringForm } from "./RecurringForm";
import { RecurringInstances } from "./RecurringInstances";
import { RecurringInput } from "./RecurringInput";

type Editor = {
    id: string;
    version: number | null;
    fields: RecurringFields | null;
    rule: RecurringRule | null;
    source: BusinessDraftSummary | null;
};
const editorFor = (rule: RecurringRule): Editor => ({
    id: rule.id,
    version: rule.version,
    fields: recurringFields(rule.anchor_date, rule),
    rule,
    source: null,
});
export function RecurringPanel({
    ledgerId,
    session,
    locale,
    controller,
    archived,
    dataLoading,
    onEditing,
    onUnauthorized,
    onOpenDraft,
}: {
    ledgerId: string;
    session: Session;
    locale: Locale;
    controller: PendingRecurringController;
    archived: boolean;
    dataLoading: boolean;
    onEditing: (editing: boolean) => void;
    onUnauthorized: () => void;
    onOpenDraft: (id: string) => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const pending = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
    const plan = pending.plan?.ledgerId === ledgerId ? pending.plan : null;
    const [editor, setEditor] = useState<Editor | null>(() =>
        plan
            ? {
                  id: plan.id,
                  version:
                      plan.action === "create" ? null : JSON.parse(plan.bodyJson).expected_version,
                  fields: null,
                  rule: null,
                  source: null,
              }
            : null,
    );
    const [offset, setOffset] = useState(0),
        [refresh, setRefresh] = useState(0);
    const [result, setResult] = useState<{ key: string; rows: RecurringRule[] } | null>(null);
    const [failure, setFailure] = useState<{ key: string; error: unknown } | null>(null);
    const [localError, setLocalError] = useState<unknown>(null),
        [reading, setReading] = useState(false);
    const key = JSON.stringify([ledgerId, offset, refresh]);
    const data = result?.key === key ? result.rows : null;
    const readError = failure?.key === key ? failure.error : null;
    const unresolved = !["idle", "confirmed", "conflict", "rejected"].includes(pending.status);
    const confirmed = !!plan && pending.status === "confirmed",
        conflict = !!plan && pending.status === "conflict";
    const callbacks = useRef({ onEditing, onUnauthorized });
    callbacks.current = { onEditing, onUnauthorized };
    const detailAbort = useRef<AbortController | null>(null);
    useEffect(() => () => detailAbort.current?.abort(), []);
    useEffect(() => {
        callbacks.current.onEditing(!!editor);
        return () => callbacks.current.onEditing(false);
    }, [editor]);
    useEffect(() => {
        if (pending.status === "auth-required") callbacks.current.onUnauthorized();
    }, [pending.status]);
    useEffect(() => {
        const abort = new AbortController();
        listRecurringRules(ledgerId, offset, abort.signal)
            .then((rows) => {
                if (!abort.signal.aborted) setResult({ key, rows });
            })
            .catch((error) => {
                if (abort.signal.aborted) return;
                if (error instanceof ApiError && error.status === 401)
                    callbacks.current.onUnauthorized();
                else setFailure({ key, error });
            });
        return () => abort.abort();
    }, [ledgerId, offset, key]);
    function fail(error: unknown) {
        if (error instanceof ApiError && error.status === 401) callbacks.current.onUnauthorized();
        else setLocalError(error);
    }
    function finish() {
        if (!controller.dismiss()) return false;
        detailAbort.current?.abort();
        setReading(false);
        setEditor(null);
        setLocalError(null);
        setRefresh((v) => v + 1);
        return true;
    }
    async function open(id: string) {
        if (!controller.dismiss() || reading) return;
        detailAbort.current?.abort();
        const abort = new AbortController();
        detailAbort.current = abort;
        setEditor({ id, version: null, fields: null, rule: null, source: null });
        setReading(true);
        setLocalError(null);
        try {
            const rule = await getRecurringRule(ledgerId, id, abort.signal);
            if (!abort.signal.aborted) setEditor(editorFor(rule));
        } catch (error) {
            if (!abort.signal.aborted) fail(error);
        } finally {
            if (!abort.signal.aborted) setReading(false);
        }
    }
    async function save(fields: RecurringFields, source: BusinessDraftSummary | null) {
        if (!["idle", "rejected"].includes(controller.getSnapshot().status)) return;
        if (!editor || unresolved || archived || dataLoading) return;
        setLocalError(null);
        setEditor({ ...editor, fields, source });
        try {
            const create = editor.version === null;
            const body = create
                ? recurringCreate(editor.id, fields, source!)
                : {
                      expected_version: editor.version!,
                      name: fields.name,
                      ...(source
                          ? { source_document_id: source.id, source_version: source.version }
                          : {}),
                  };
            await controller.start(session, ledgerId, editor.id, create ? "create" : "save", body);
        } catch (error) {
            fail(error);
        }
    }
    async function archiveRule() {
        if (!["idle", "rejected"].includes(controller.getSnapshot().status)) return;
        if (!editor?.rule || unresolved || archived || dataLoading) return;
        setLocalError(null);
        try {
            await controller.start(session, ledgerId, editor.id, "archive", {
                expected_version: editor.rule.version,
                archived: !editor.rule.archived,
            });
        } catch (error) {
            fail(error);
        }
    }
    function problem(error: unknown) {
        if (
            error instanceof ApiError &&
            error.code === "version_conflict" &&
            plan?.action === "create"
        )
            return t(
                "源草稿已修改。当前选择与输入已保留；刷新模板列表并重新选择当前版本后再保存。",
                "The source draft changed. Input and selection are retained; refresh the template list and choose its current version before saving.",
            );
        if (error instanceof ApiError && error.code === "recurrence_timezone")
            return t(
                "请输入有效的IANA时区名称，例如Asia/Shanghai或UTC。",
                "Enter a valid IANA timezone name, such as Asia/Shanghai or UTC.",
            );
        return businessError(error, locale);
    }
    const original = plan ? JSON.parse(plan.bodyJson) : null;
    const frequency = (value: string) =>
        ({
            day: t("日", "day"),
            week: t("周", "week"),
            month: t("月", "month"),
            year: t("年", "year"),
        })[value] ?? value;
    return (
        <section
            className="business-panel draft-panel"
            aria-label={t("周期Invoice", "Recurring invoices")}
        >
            <div className="section-heading">
                <h2>{t("周期Invoice", "Recurring invoices")}</h2>
                <button
                    type="button"
                    disabled={!!editor || unresolved}
                    onClick={() => setRefresh((v) => v + 1)}
                >
                    {t("刷新规则", "Refresh rules")}
                </button>
            </div>
            <p className="help-text">
                {t(
                    "维护任务只生成草稿。暂停后恢复会补齐首个未生成日期起的积压；需要跳过旧周期时请用新起点创建替代规则。刷新或关闭页面会丢失未保存输入和内存中的待确认请求。",
                    "Maintenance jobs create drafts only. Resuming catches up from the first ungenerated date; use a replacement rule with a new anchor to skip older periods. Reloading or closing the page loses unsaved input and in-memory pending requests.",
                )}
            </p>
            {archived && (
                <p role="status">
                    {t("主体已归档，仅可查看。", "This entity is archived; rules are read-only.")}
                </p>
            )}
            {readError != null && <p role="alert">{problem(readError)}</p>}
            {editor ? (
                <>
                    {reading && <p role="status">{t("正在读取规则…", "Loading rule…")}</p>}
                    {confirmed ? (
                        <>
                            <p role="status">
                                {t("规则已保存并核对。", "Rule saved and verified.")}
                            </p>
                            <button type="button" onClick={finish}>
                                {t("返回规则列表", "Back to rules")}
                            </button>
                        </>
                    ) : (
                        <>
                            {editor.rule && (
                                <p>
                                    {editor.rule.name} · {t("版本", "Version")}{" "}
                                    {editor.rule.version} · {editor.rule.timezone_name} ·{" "}
                                    {t("下次计划日期", "Next scheduled date")}:{" "}
                                    {editor.rule.next_scheduled_date ??
                                        t("超出日历范围", "Calendar exhausted")}
                                </p>
                            )}
                            {editor.rule && (
                                <RecurringInput
                                    value={editor.rule.template_input}
                                    locale={locale}
                                    template
                                />
                            )}
                            {plan && (unresolved || conflict) && (
                                <p data-testid="recurring-original-intent">
                                    {t("原请求已保留", "Original request retained")}:{" "}
                                    {original.name ?? t("暂停 / 恢复", "Pause / resume")}{" "}
                                    {original.anchor_date ?? ""} {original.timezone_name ?? ""}{" "}
                                    {original.source_version
                                        ? `· ${t("源版本", "Source version")} ${original.source_version}`
                                        : ""}
                                </p>
                            )}
                            {editor.fields &&
                                !unresolved &&
                                !conflict &&
                                !editor.rule?.archived && (
                                    <RecurringForm
                                        key={`${editor.id}:${editor.version}`}
                                        ledgerId={ledgerId}
                                        initial={editor.fields}
                                        initialSource={editor.source}
                                        existing={editor.version !== null}
                                        locale={locale}
                                        locked={archived || dataLoading}
                                        canFinish={!unresolved}
                                        onSave={save}
                                        onCancel={finish}
                                        onUnauthorized={onUnauthorized}
                                    />
                                )}
                            {editor.rule && !unresolved && !conflict && (
                                <div className="form-actions">
                                    <button
                                        type="button"
                                        disabled={archived || dataLoading}
                                        onClick={() => void archiveRule()}
                                    >
                                        {editor.rule.archived
                                            ? t("恢复规则并补齐积压", "Resume rule and catch up")
                                            : t("暂停规则", "Pause rule")}
                                    </button>
                                    {editor.rule.archived && (
                                        <button type="button" onClick={finish}>
                                            {t("返回规则列表", "Back to rules")}
                                        </button>
                                    )}
                                </div>
                            )}
                            {pending.status === "submitting" && (
                                <p role="status">{t("正在保存…", "Saving…")}</p>
                            )}
                            {plan && pending.status === "unknown" && (
                                <div role="alert">
                                    <p>
                                        {t(
                                            "结果尚未确认；请重试原请求，不要重新创建规则。",
                                            "The result is unconfirmed. Retry the original request instead of creating another rule.",
                                        )}
                                    </p>
                                    <button
                                        type="button"
                                        onClick={() => void controller.retry(session).catch(fail)}
                                    >
                                        {t("重试原规则请求", "Retry original rule request")}
                                    </button>
                                </div>
                            )}
                            {conflict && (
                                <div role="alert">
                                    <p>
                                        {t(
                                            "规则已变化（后台生成也会更新版本）。原输入仍保留；重新载入会替换为当前记录，请核对后再决定。",
                                            "The rule changed; generation also updates its version. Original input is retained. Reload replaces it with the current record for your review.",
                                        )}
                                    </p>
                                    <button type="button" onClick={() => void open(editor.id)}>
                                        {t("重新载入当前规则", "Reload current rule")}
                                    </button>
                                    <button type="button" onClick={finish}>
                                        {t("返回规则列表", "Back to rules")}
                                    </button>
                                </div>
                            )}
                            {pending.status === "rejected" && pending.error && (
                                <p role="alert">{problem(pending.error)}</p>
                            )}
                            {!editor.fields && !unresolved && !conflict && (
                                <div className="form-actions">
                                    <button
                                        type="button"
                                        disabled={reading}
                                        onClick={() => void open(editor.id)}
                                    >
                                        {t("重试读取规则", "Retry loading rule")}
                                    </button>
                                    <button type="button" onClick={finish}>
                                        {t("返回规则列表", "Back to rules")}
                                    </button>
                                </div>
                            )}
                            {editor.rule && (
                                <RecurringInstances
                                    key={editor.id}
                                    ledgerId={ledgerId}
                                    ruleId={editor.id}
                                    locale={locale}
                                    locked={unresolved}
                                    onUnauthorized={onUnauthorized}
                                    onOpenDraft={(id) => {
                                        if (finish()) onOpenDraft(id);
                                    }}
                                />
                            )}
                        </>
                    )}
                    {localError != null && <p role="alert">{problem(localError)}</p>}
                </>
            ) : (
                <>
                    <button
                        type="button"
                        disabled={dataLoading || archived || unresolved || !data}
                        onClick={() => {
                            if (!controller.dismiss()) return;
                            setEditor({
                                id: crypto.randomUUID(),
                                version: null,
                                fields: recurringFields(localDraftDate()),
                                rule: null,
                                source: null,
                            });
                            setLocalError(null);
                        }}
                    >
                        {t("新建周期规则", "New recurring rule")}
                    </button>
                    {!data && !readError && <p role="status">{t("正在读取…", "Loading…")}</p>}
                    {data?.length === 0 && (
                        <p>{t("此页暂无周期规则。", "No recurring rules on this page.")}</p>
                    )}
                    <ul className="master-records">
                        {data?.map((rule) => (
                            <li key={rule.id} data-testid="recurring-rule">
                                <strong>{rule.name}</strong>
                                <span>
                                    {rule.archived ? t("已暂停", "Paused") : t("启用", "Active")} ·{" "}
                                    {rule.interval_count ?? 1} {frequency(rule.frequency)} ·{" "}
                                    {rule.timezone_name}
                                </span>
                                <span>
                                    {t("下次计划日期", "Next scheduled date")}:{" "}
                                    {rule.next_scheduled_date ??
                                        t("超出日历范围", "Calendar exhausted")}{" "}
                                    · {t("已生成", "Generated")} {rule.next_index}
                                </span>
                                <button type="button" onClick={() => void open(rule.id)}>
                                    {t("查看周期规则", "View recurring rule")}
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
                            {t("上一页", "Previous page")}
                        </button>
                        <span>
                            {t("页", "Page")} {offset / 25 + 1}
                        </span>
                        <button
                            type="button"
                            disabled={data?.length !== 25 || offset >= 100000}
                            onClick={() => setOffset((v) => v + 25)}
                        >
                            {t("下一页", "Next page")}
                        </button>
                    </div>
                </>
            )}
        </section>
    );
}
