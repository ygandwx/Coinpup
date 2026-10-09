import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import type { Locale } from "./i18n";
import type { Asset, Category } from "./ledger-api";
import { listMasterData } from "./business-api";
import type { Party, Project } from "./business-api";
import { getBusinessDraft, listBusinessDrafts } from "./business-draft-api";
import type {
    BusinessDraft,
    BusinessDraftCreate,
    BusinessDraftSummary,
} from "./business-draft-api";
import type { PendingBusinessDraftController } from "./pending-business-drafts";
import { businessError } from "./business-errors";
import { draftFields, localDraftDate, newDraft } from "./draft-fields";
import type { DraftFields } from "./draft-fields";
import { DraftForm } from "./DraftForm";
import { formatAmount } from "./money";

type Editor = {
    id: string;
    version: number | null;
    fields: DraftFields | null;
    record: BusinessDraft | null;
};
const editorFor = (row: BusinessDraft): Editor => ({
    id: row.id,
    version: row.version,
    fields: draftFields(row),
    record: row,
});
async function allMasters(ledger: string, kind: "parties" | "projects", signal: AbortSignal) {
    const rows = [];
    for (let offset = 0; offset <= 100000; offset += 25) {
        const page = await listMasterData(ledger, kind, offset, signal);
        rows.push(...page);
        if (page.length < 25) return rows;
    }
    throw new ApiError("server", null, "reference_limit");
}
export function BusinessDraftPanel({
    ledgerId,
    session,
    locale,
    controller,
    assets,
    categories,
    archived,
    dataLoading,
    onEditing,
    onUnauthorized,
}: {
    ledgerId: string;
    session: Session;
    locale: Locale;
    controller: PendingBusinessDraftController;
    assets: Asset[];
    categories: Category[];
    archived: boolean;
    dataLoading: boolean;
    onEditing: (editing: boolean) => void;
    onUnauthorized: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const pending = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
    const plan = pending.plan?.ledgerId === ledgerId ? pending.plan : null;
    const [editor, setEditor] = useState<Editor | null>(() => {
        if (!plan) return null;
        const body = JSON.parse(plan.bodyJson);
        return {
            id: plan.id,
            version: plan.action === "create" ? null : body.expected_version,
            fields: plan.action === "archive" ? null : draftFields(body as BusinessDraftCreate),
            record: null,
        };
    });
    const [offset, setOffset] = useState(0),
        [refresh, setRefresh] = useState(0);
    const [result, setResult] = useState<{
        key: string;
        rows: BusinessDraftSummary[];
        parties: Party[];
        projects: Project[];
    } | null>(null);
    const [failure, setFailure] = useState<{
        key: string;
        error: unknown;
    } | null>(null);
    const [localError, setLocalError] = useState<unknown>(null);
    const [reading, setReading] = useState(false);
    const key = JSON.stringify([ledgerId, offset, refresh]);
    const data = result?.key === key ? result : null;
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
        Promise.all([
            listBusinessDrafts(ledgerId, offset, abort.signal),
            allMasters(ledgerId, "parties", abort.signal),
            allMasters(ledgerId, "projects", abort.signal),
        ])
            .then(([rows, parties, projects]) => {
                if (!abort.signal.aborted)
                    setResult({
                        key,
                        rows,
                        parties: parties as Party[],
                        projects: projects as Project[],
                    });
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
        if (!controller.dismiss()) return;
        detailAbort.current?.abort();
        setReading(false);
        setEditor(null);
        setLocalError(null);
        setRefresh((value) => value + 1);
    }
    function edit(row?: BusinessDraft) {
        if (!controller.dismiss()) return;
        setEditor(
            row
                ? editorFor(row)
                : {
                      id: crypto.randomUUID(),
                      version: null,
                      fields: newDraft(localDraftDate()),
                      record: null,
                  },
        );
        setLocalError(null);
    }
    async function open(id: string) {
        if (!controller.dismiss() || reading) return;
        detailAbort.current?.abort();
        const abort = new AbortController();
        detailAbort.current = abort;
        setEditor({ id, version: null, fields: null, record: null });
        setReading(true);
        setLocalError(null);
        try {
            const row = await getBusinessDraft(ledgerId, id, abort.signal);
            if (!abort.signal.aborted) setEditor(editorFor(row));
        } catch (error) {
            if (!abort.signal.aborted) fail(error);
        } finally {
            if (!abort.signal.aborted) setReading(false);
        }
    }
    async function save(fields: DraftFields, refreshSnapshots: boolean) {
        if (dataLoading || !["idle", "rejected"].includes(controller.getSnapshot().status)) return;
        if (!editor || unresolved || confirmed || conflict || archived) return;
        const asset = assets.find((row) => row.asset_id === fields.asset_id);
        if (!asset) return;
        setLocalError(null);
        try {
            await controller.start(
                session,
                ledgerId,
                editor.id,
                editor.version === null ? "create" : "save",
                asset,
                editor.version === null
                    ? { ...fields, id: editor.id }
                    : {
                          ...fields,
                          expected_version: editor.version,
                          refresh_snapshots: refreshSnapshots,
                      },
            );
        } catch (error) {
            fail(error);
        }
    }
    async function archive() {
        if (!["idle", "rejected"].includes(controller.getSnapshot().status)) return;
        const row = editor?.record;
        if (!row || unresolved || confirmed || conflict || archived) return;
        const asset = assets.find((item) => item.asset_id === row.asset_id);
        if (!asset) return;
        setLocalError(null);
        try {
            await controller.start(session, ledgerId, row.id, "archive", asset, {
                expected_version: row.version,
                archived: !row.archived,
            });
        } catch (error) {
            fail(error);
        }
    }
    async function retry() {
        if (controller.getSnapshot().status !== "unknown") return;
        setLocalError(null);
        try {
            await controller.retry(session);
        } catch (error) {
            fail(error);
        }
    }
    return (
        <section
            className="business-panel draft-panel"
            aria-label={t("经营草稿", "Business drafts")}
        >
            <div className="section-heading">
                <h2>{t("经营草稿", "Business drafts")}</h2>
                <button
                    type="button"
                    disabled={!!editor || unresolved}
                    onClick={() => setRefresh((value) => value + 1)}
                >
                    {t("刷新草稿", "Refresh drafts")}
                </button>
            </div>
            <p className="help-text">
                {t(
                    "保存草稿不会入账。未解决提交请重试原请求；刷新或关闭页面会丢失未保存输入及内存意图。",
                    "Saving drafts does not post money. Retry unresolved submissions with the original request. Reloading or closing the page loses unsaved input and in-memory intent.",
                )}
            </p>
            {archived && (
                <p role="status">
                    {t("主体已归档，仅可查看。", "This entity is archived; drafts are read-only.")}
                </p>
            )}
            {readError != null && <p role="alert">{businessError(readError, locale)}</p>}
            {editor ? (
                <>
                    <p className="help-text">
                        {t("单据", "Document")}: {editor.id} · {t("版本", "Version")}:{" "}
                        {editor.version ?? t("新建", "New")}
                    </p>
                    {confirmed ? (
                        <>
                            <p role="status">
                                {t("草稿已保存并核对。", "Draft saved and verified.")}
                            </p>
                            <button type="button" onClick={finish}>
                                {t("返回草稿列表", "Back to drafts")}
                            </button>
                        </>
                    ) : (
                        <>
                            {editor.record && (
                                <p className="help-text">
                                    {t("保存时的公司 / 往来快照", "Saved issuer / party snapshots")}
                                    : {String(editor.record.issuer_snapshot.name ?? "—")} /{" "}
                                    {String(editor.record.party_snapshot.name ?? "—")}
                                </p>
                            )}
                            {editor.record && editor.record.lines.length > 0 && (
                                <details className="help-text">
                                    <summary>
                                        {t(
                                            "保存时的分类 / 项目快照",
                                            "Saved category / project snapshots",
                                        )}
                                    </summary>
                                    <ul>
                                        {editor.record.lines.map((line, index) => (
                                            <li key={line.id}>
                                                {index + 1}.{" "}
                                                {String(
                                                    line.category_snapshot.name ?? line.category_id,
                                                )}{" "}
                                                / {String(line.project_snapshot?.name ?? "—")}
                                            </li>
                                        ))}
                                    </ul>
                                </details>
                            )}
                            {editor.fields && data ? (
                                <DraftForm
                                    key={`${editor.id}:${editor.version}`}
                                    initial={editor.fields}
                                    refs={{
                                        assets,
                                        categories,
                                        parties: data.parties,
                                        projects: data.projects,
                                    }}
                                    locale={locale}
                                    existing={editor.version !== null}
                                    locked={
                                        dataLoading ||
                                        unresolved ||
                                        conflict ||
                                        archived ||
                                        !!editor.record?.archived
                                    }
                                    canFinish={!unresolved}
                                    onSave={save}
                                    onCancel={finish}
                                />
                            ) : (
                                <>
                                    {reading && <p role="status">{t("正在读取…", "Loading…")}</p>}
                                    <button type="button" disabled={unresolved} onClick={finish}>
                                        {t("返回草稿列表", "Back to drafts")}
                                    </button>
                                </>
                            )}
                            {editor.record && (
                                <>
                                    <p className="help-text">
                                        {t(
                                            "归档或恢复只作用于已保存版本，不会保存当前表单修改。",
                                            "Archive or restore applies to the saved version; it does not save current form edits.",
                                        )}
                                    </p>
                                    <button
                                        type="button"
                                        disabled={unresolved || conflict || archived}
                                        onClick={archive}
                                    >
                                        {editor.record.archived
                                            ? t("恢复草稿", "Restore draft")
                                            : t("归档草稿", "Archive draft")}
                                    </button>
                                </>
                            )}
                            {conflict && (
                                <div role="alert">
                                    <p>
                                        {t(
                                            "版本或内容已变化，原修改未被确认为成功。请核对后显式载入当前草稿。",
                                            "The version or content changed; the original edit is not confirmed. Review and explicitly load the current draft.",
                                        )}
                                    </p>
                                    <button
                                        type="button"
                                        disabled={!pending.record}
                                        onClick={() => pending.record && edit(pending.record)}
                                    >
                                        {t("载入当前草稿", "Load current draft")}
                                    </button>
                                </div>
                            )}
                            {pending.status === "unknown" && (
                                <div role="alert">
                                    <p>
                                        {t(
                                            "提交结果未知，输入已锁定；不要创建重复单据。",
                                            "The submission result is unknown. Input is locked; do not create a duplicate document.",
                                        )}
                                    </p>
                                    <button type="button" onClick={retry}>
                                        {t("重试原请求", "Retry original request")}
                                    </button>
                                </div>
                            )}
                            {pending.status === "submitting" && (
                                <p role="status">{t("正在保存…", "Saving…")}</p>
                            )}
                            {pending.status === "rejected" && pending.error && (
                                <p role="alert">{businessError(pending.error, locale)}</p>
                            )}
                        </>
                    )}
                    {localError != null && <p role="alert">{businessError(localError, locale)}</p>}
                </>
            ) : (
                <>
                    <button
                        type="button"
                        disabled={dataLoading || archived || unresolved || !data}
                        onClick={() => edit()}
                    >
                        {t("新建经营草稿", "New business draft")}
                    </button>
                    {!data && !readError && <p role="status">{t("正在读取…", "Loading…")}</p>}
                    {data && (
                        <ul className="master-records">
                            {data.rows.map((row) => (
                                <li key={row.id}>
                                    <strong>
                                        {row.document_kind === "invoice"
                                            ? t("销售 Invoice", "Sales invoice")
                                            : t("采购账单", "Purchase bill")}{" "}
                                        · {String(row.party_snapshot.name ?? row.party_id)}
                                    </strong>
                                    <span>
                                        {row.issue_date} ·{" "}
                                        {row.archived
                                            ? t("已归档", "Archived")
                                            : t("草稿", "Draft")}{" "}
                                        · {t("版本", "Version")} {row.version}
                                    </span>
                                    <span>
                                        {formatAmount(row.total_amount, locale)} {row.asset_id}
                                    </span>
                                    <button type="button" onClick={() => open(row.id)}>
                                        {t("查看草稿", "View draft")}
                                    </button>
                                </li>
                            ))}
                        </ul>
                    )}
                    {data?.rows.length === 0 && (
                        <p>{t("此页暂无草稿。", "No drafts on this page.")}</p>
                    )}
                    <div className="form-actions">
                        <button
                            type="button"
                            disabled={!data || offset === 0}
                            onClick={() => setOffset((value) => value - 25)}
                        >
                            {t("上一页", "Previous page")}
                        </button>
                        <span>
                            {t("页", "Page")} {offset / 25 + 1}
                        </span>
                        <button
                            type="button"
                            disabled={data?.rows.length !== 25 || offset >= 100000}
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
