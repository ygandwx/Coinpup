import { useEffect, useId, useRef, useState, useSyncExternalStore } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import { listMasterData } from "./business-api";
import type { MasterBody, MasterKind, MasterRecord, PartyCreate } from "./business-api";
import { businessError } from "./business-errors";
import type { Locale } from "./i18n";
import { MasterForm } from "./MasterForm";
import type { MasterFields } from "./MasterForm";
import type { PendingMasterDataController } from "./pending-business";

type Editor = { id: string; version: number | null; fields: MasterFields };
export function MasterPanel({
    ledgerId,
    locale,
    session,
    controller,
    archived,
    onUnauthorized,
    onEditing,
}: {
    ledgerId: string;
    locale: Locale;
    session: Session;
    controller: PendingMasterDataController;
    archived: boolean;
    onUnauthorized: () => void;
    onEditing: (editing: boolean) => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const kindId = useId();
    const pending = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
    const plan = pending.plan?.ledgerId === ledgerId ? pending.plan : null;
    const [kind, setKind] = useState<MasterKind>(plan?.kind ?? "parties");
    const [editor, setEditor] = useState<Editor | null>(() => {
        if (!plan) return null;
        const body = JSON.parse(plan.bodyJson);
        return {
            id: plan.id,
            version: plan.method === "POST" ? null : body.expected_version,
            fields: body,
        };
    });
    const [offset, setOffset] = useState(0);
    const [refresh, setRefresh] = useState(0);
    const [failure, setFailure] = useState<{ key: string; error: unknown } | null>(null);
    const [result, setResult] = useState<{ key: string; rows: MasterRecord[] } | null>(null);
    const [localError, setLocalError] = useState<unknown>(null);
    const key = JSON.stringify([ledgerId, kind, offset, refresh]);
    const rows = result?.key === key ? result.rows : null;
    const readError = failure?.key === key ? failure.error : null;
    const unresolved = !["idle", "confirmed", "conflict", "rejected"].includes(pending.status);
    const confirmed = !!plan && pending.status === "confirmed";
    const conflict = !!plan && pending.status === "conflict";
    const callbacks = useRef({ onUnauthorized, onEditing });
    callbacks.current = { onUnauthorized, onEditing };
    useEffect(() => {
        callbacks.current.onEditing(!!editor);
        return () => callbacks.current.onEditing(false);
    }, [editor]);
    useEffect(() => {
        if (pending.status === "auth-required") callbacks.current.onUnauthorized();
    }, [pending.status]);
    useEffect(() => {
        const abort = new AbortController();
        listMasterData(ledgerId, kind, offset, abort.signal)
            .then((items) => {
                if (!abort.signal.aborted) setResult({ key, rows: items });
            })
            .catch((error) => {
                if (abort.signal.aborted) return;
                if (error instanceof ApiError && error.status === 401)
                    callbacks.current.onUnauthorized();
                else setFailure({ key, error });
            });
        return () => abort.abort();
    }, [ledgerId, kind, offset, key]);
    function finish() {
        if (!controller.dismiss()) return;
        setEditor(null);
        setLocalError(null);
        setRefresh((value) => value + 1);
    }
    function edit(row?: MasterRecord) {
        if (!controller.dismiss()) return;
        setEditor(
            row
                ? { id: row.id, version: row.version, fields: row }
                : { id: crypto.randomUUID(), version: null, fields: {} },
        );
        setLocalError(null);
    }
    async function save(fields: Omit<PartyCreate, "id"> & { archived?: boolean }) {
        if (!editor || unresolved || confirmed || conflict || archived) return;
        setLocalError(null);
        const detail =
            kind === "projects"
                ? {
                      name: fields.name,
                      notes: fields.notes,
                      ...(editor.version === null ? {} : { archived: fields.archived }),
                  }
                : fields;
        const body: MasterBody =
            editor.version === null
                ? { ...detail, id: editor.id }
                : { ...detail, expected_version: editor.version };
        try {
            await controller.start(
                session,
                ledgerId,
                kind,
                editor.id,
                editor.version === null ? "POST" : "PATCH",
                body,
            );
        } catch (error) {
            setLocalError(error);
        }
    }
    async function retry() {
        setLocalError(null);
        try {
            await controller.retry(session);
        } catch (error) {
            setLocalError(error);
        }
    }
    return (
        <section
            className="business-panel master-panel"
            aria-label={t("往来与项目", "Parties and projects")}
        >
            <div className="section-heading">
                <h2>{t("往来与项目", "Parties and projects")}</h2>
                <button
                    type="button"
                    disabled={!!editor || unresolved}
                    onClick={() => setRefresh((value) => value + 1)}
                >
                    {t("刷新资料", "Refresh records")}
                </button>
            </div>
            <p className="help-text">
                {t(
                    "资料独立属于当前账本；归档保留历史，保存资料不会入账。",
                    "Records belong to this ledger. Archiving preserves history; saving details does not post money.",
                )}
            </p>
            <div className="master-field">
                <label htmlFor={kindId}>{t("资料类型", "Record type")}</label>
                <select
                    id={kindId}
                    value={kind}
                    disabled={!!editor || unresolved}
                    onChange={(event) => {
                        setKind(event.target.value as MasterKind);
                        setOffset(0);
                    }}
                >
                    <option value="parties">{t("往来单位", "Business parties")}</option>
                    <option value="projects">{t("项目", "Projects")}</option>
                </select>
            </div>
            {archived && (
                <p role="status">
                    {t(
                        "主体已归档，只能查看资料。",
                        "This entity is archived. Records are read-only.",
                    )}
                </p>
            )}
            {editor ? (
                <>
                    <p className="help-text">
                        {t("记录", "Record")}: {editor.id} · {t("版本", "Version")}:{" "}
                        {editor.version ?? t("新建", "New")}
                    </p>
                    {confirmed ? (
                        <>
                            <p role="status">
                                {t("资料已保存并核对。", "Record saved and verified.")}
                            </p>
                            <button type="button" onClick={finish}>
                                {t("返回资料列表", "Back to records")}
                            </button>
                        </>
                    ) : (
                        <>
                            <MasterForm
                                key={`${editor.id}:${editor.version}`}
                                initial={editor.fields}
                                kind={kind}
                                locale={locale}
                                existing={editor.version !== null}
                                locked={unresolved || conflict || archived}
                                canFinish={!unresolved}
                                onSave={save}
                                onCancel={finish}
                            />
                            {conflict && (
                                <div role="alert">
                                    <p>
                                        {t(
                                            "版本或内容已变化，无法确认原修改是否保存。核对后显式载入当前资料，或结束编辑。",
                                            "The version or content changed; the original edit is not confirmed. Review and explicitly load the current record, or finish editing.",
                                        )}
                                    </p>
                                    <button
                                        type="button"
                                        onClick={() => pending.record && edit(pending.record)}
                                        disabled={!pending.record || archived}
                                    >
                                        {t("载入当前资料", "Load current record")}
                                    </button>
                                </div>
                            )}
                            {pending.status === "unknown" && (
                                <div role="alert">
                                    <p>
                                        {t(
                                            "保存结果未知，输入已锁定。请重试原请求；不要新建重复记录。关闭页面会丢失内存中的待核对意图。",
                                            "The save result is unknown. Input is locked. Retry the original request; do not create a duplicate. Closing the page loses the pending intent.",
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
                            {localError != null && (
                                <p role="alert">{businessError(localError, locale)}</p>
                            )}
                        </>
                    )}
                </>
            ) : (
                <>
                    <button type="button" disabled={archived || unresolved} onClick={() => edit()}>
                        {t("新增资料", "New record")}
                    </button>
                    {readError != null && <p role="alert">{businessError(readError, locale)}</p>}
                    {!rows && !readError && <p role="status">{t("正在读取…", "Loading…")}</p>}
                    {rows && (
                        <ul className="master-records">
                            {rows.map((row) => (
                                <li key={row.id}>
                                    <strong>
                                        {row.name ??
                                            t("未补全的往来资料", "Incomplete party profile")}
                                    </strong>
                                    <span>
                                        {t("版本", "Version")} {row.version} ·{" "}
                                        {row.archived
                                            ? t("已归档", "Archived")
                                            : t("使用中", "Active")}
                                    </span>
                                    {"role" in row && (
                                        <span>
                                            {row.role === "customer"
                                                ? t("客户", "Customer")
                                                : row.role === "supplier"
                                                  ? t("供应商", "Supplier")
                                                  : row.role === "both"
                                                    ? t("客户与供应商", "Customer and supplier")
                                                    : "—"}
                                        </span>
                                    )}
                                    <p>{row.notes}</p>
                                    <button type="button" onClick={() => edit(row)}>
                                        {archived
                                            ? t("查看资料", "View record")
                                            : t("查看与编辑", "View and edit")}
                                    </button>
                                </li>
                            ))}
                        </ul>
                    )}
                    {rows?.length === 0 && <p>{t("此页暂无资料。", "No records on this page.")}</p>}
                    <div className="form-actions">
                        <button
                            type="button"
                            disabled={offset === 0 || !rows}
                            onClick={() => setOffset((value) => value - 25)}
                        >
                            {t("上一页", "Previous page")}
                        </button>
                        <span>
                            {t("页", "Page")} {offset / 25 + 1}
                        </span>
                        <button
                            type="button"
                            disabled={rows?.length !== 25 || offset >= 100000}
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
