import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import type { Asset, Category } from "./ledger-api";
import type { Locale } from "./i18n";
import { getDocumentCurrent } from "./document-revision-api";
import type { DocumentCurrent } from "./document-revision-api";
import { draftFields } from "./draft-fields";
import { documentRevisionDisplay } from "./document-revision-display";
import { isDocumentRevisionPlan } from "./document-revision-intent";
import type { PendingBusinessConfirmationController } from "./pending-business-confirmation";
import {
    checkedDocumentCurrent,
    loadDocumentRevisionEditor,
} from "./document-revision-review-loader";
import type { DocumentRevisionEditor } from "./document-revision-review-loader";
import type { DocumentRevisionFields } from "./document-revision-fields";
import { BusinessDocumentCurrentFacts } from "./BusinessDocumentCurrentFacts";
import { BusinessDocumentRevisionForm } from "./BusinessDocumentRevisionForm";
import {
    BusinessDocumentRevisionResult,
    documentRevisionError,
} from "./BusinessDocumentRevisionResult";

export function BusinessDocumentRevisionPanel({
    ledgerId,
    documentId,
    session,
    locale,
    controller,
    assets,
    categories,
    archived,
    dataLoading,
    onUnauthorized,
    onKeepTarget,
    onBack,
}: {
    ledgerId: string;
    documentId: string;
    session: Session;
    locale: Locale;
    controller: PendingBusinessConfirmationController;
    assets: Asset[];
    categories: Category[];
    archived: boolean;
    dataLoading: boolean;
    onUnauthorized: () => void;
    onKeepTarget: () => void;
    onBack: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const pending = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
    const plan =
        isDocumentRevisionPlan(pending.plan) &&
        pending.plan.ownerId === String(session.user.id) &&
        pending.plan.ledgerId === ledgerId &&
        pending.plan.documentId === documentId
            ? pending.plan
            : null;
    const [refresh, setRefresh] = useState(0);
    const request = useMemo(
        () => ({ ledgerId, documentId, session, refresh }),
        [ledgerId, documentId, session, refresh],
    );
    const [loaded, setLoaded] = useState<{
        request: typeof request;
        current: DocumentCurrent;
    } | null>(null);
    const [failure, setFailure] = useState<{ request: typeof request; error: unknown } | null>(
        null,
    );
    const [editor, setEditor] = useState<
        (DocumentRevisionEditor & { request: typeof request }) | null
    >(null);
    const [ready, setReady] = useState<typeof request | null>(null);
    const [busy, setBusy] = useState(false);
    const active = useRef(false),
        generation = useRef(0),
        sourceAbort = useRef<AbortController | null>(null);
    const latest = useRef({ request, assets, categories, onUnauthorized });
    latest.current = { request, assets, categories, onUnauthorized };
    useEffect(() => {
        active.current = true;
        setBusy(false);
        setReady(null);
        return () => {
            active.current = false;
            generation.current += 1;
            sourceAbort.current?.abort();
        };
    }, [request]);
    useEffect(() => {
        if (pending.plan || pending.status === "auth-required" || editor || dataLoading) return;
        const abort = new AbortController();
        getDocumentCurrent(ledgerId, documentId, abort.signal)
            .then((value) => {
                if (abort.signal.aborted || latest.current.request !== request) return;
                const current = checkedDocumentCurrent(
                    value,
                    ledgerId,
                    documentId,
                    latest.current.assets,
                );
                setLoaded({ request, current });
                setFailure(null);
            })
            .catch((error) => {
                if (abort.signal.aborted || latest.current.request !== request) return;
                if (error instanceof ApiError && error.status === 401)
                    latest.current.onUnauthorized();
                else setFailure({ request, error });
            });
        return () => abort.abort();
    }, [ledgerId, documentId, request, pending.plan, pending.status, editor, dataLoading]);
    function fail(error: unknown) {
        if (!active.current || latest.current.request !== request) return;
        if (error instanceof ApiError && error.status === 401) latest.current.onUnauthorized();
        else setFailure({ request, error });
    }
    async function prepare(
        action: "correct" | "cancel",
        preserve?: DocumentRevisionFields,
        expectedVersion?: number,
    ) {
        if (controller.getSnapshot().plan || archived || dataLoading) return;
        const token = ++generation.current;
        sourceAbort.current?.abort();
        const abort = new AbortController();
        sourceAbort.current = abort;
        setBusy(true);
        setReady(null);
        setFailure(null);
        try {
            const value = await loadDocumentRevisionEditor(
                ledgerId,
                documentId,
                action,
                latest.current.assets,
                latest.current.categories,
                abort.signal,
                expectedVersion,
            );
            if (
                abort.signal.aborted ||
                token !== generation.current ||
                latest.current.request !== request
            )
                return;
            setEditor({ ...value, request, fields: preserve ?? value.fields });
            setReady(request);
        } catch (error) {
            if (
                !abort.signal.aborted &&
                token === generation.current &&
                latest.current.request === request
            )
                fail(error);
        } finally {
            if (token === generation.current && latest.current.request === request) setBusy(false);
        }
    }
    function backToCurrent() {
        generation.current += 1;
        sourceAbort.current?.abort();
        setBusy(false);
        setEditor(null);
        setReady(null);
        setFailure(null);
        setRefresh((v) => v + 1);
    }
    function editRejected() {
        const saved = controller.getSnapshot();
        if (
            saved.status !== "rejected" ||
            !isDocumentRevisionPlan(saved.plan) ||
            saved.plan.ownerId !== String(session.user.id) ||
            saved.plan.ledgerId !== ledgerId ||
            saved.plan.documentId !== documentId
        )
            return;
        const data = documentRevisionDisplay(saved.plan, "rejected", null);
        if (!data) return;
        const fields: DocumentRevisionFields = {
            action: data.input.action,
            reason: data.input.body.reason,
            replacement:
                data.input.action === "correct"
                    ? draftFields({ ...data.input.body.replacement, id: documentId })
                    : null,
            refreshReferences: false,
        };
        const references = data.review.references;
        const refAssets = [
            data.review.oldAsset,
            ...(data.review.newAsset &&
            data.review.newAsset.asset_id !== data.review.oldAsset.asset_id
                ? [data.review.newAsset]
                : []),
        ];
        onKeepTarget();
        if (!controller.dismiss()) return;
        // Preserve rejected input for the user's new review, never send it automatically.
        setEditor({
            request,
            current: data.review.current,
            fields,
            refs: {
                assets: refAssets,
                parties: references.filter((r) => r.kind === "party").map((r) => r.record),
                projects: references.filter((r) => r.kind === "project").map((r) => r.record),
                categories: references.filter((r) => r.kind === "category").map((r) => r.record),
            },
        });
        void prepare(saved.plan.action, fields);
    }
    const current = loaded?.request === request ? loaded.current : null;
    const error = failure?.request === request ? failure.error : null;
    if (pending.status === "auth-required")
        return (
            <p role="alert">
                {t(
                    "请以原用户登录后手动恢复。",
                    "Sign in as the original user to resume manually.",
                )}
            </p>
        );
    if (plan)
        return (
            <BusinessDocumentRevisionResult
                pending={pending}
                locale={locale}
                onRetry={() => {
                    void controller.retry(session).catch(fail);
                }}
                onEdit={editRejected}
                onClose={() => {
                    if (controller.dismiss()) onBack();
                }}
            />
        );
    if (pending.plan) return null;
    return (
        <section
            className="document-revision-form"
            aria-label={t("单据当前状态与修订", "Document current state and revisions")}
        >
            {editor ? (
                <>
                    <BusinessDocumentRevisionForm
                        current={editor.current}
                        fields={editor.fields}
                        refs={editor.refs}
                        locale={locale}
                        available={editor.request === request}
                        disabled={archived || dataLoading || busy || ready !== request}
                        onChange={(update) =>
                            setEditor((v) => (v ? { ...v, fields: update(v.fields) } : v))
                        }
                        onSubmit={(value) => {
                            if (
                                archived ||
                                dataLoading ||
                                busy ||
                                ready !== request ||
                                controller.getSnapshot().plan
                            )
                                return;
                            void controller
                                .startRevision(
                                    session,
                                    ledgerId,
                                    documentId,
                                    value.input,
                                    value.review,
                                )
                                .catch(fail);
                        }}
                        onBack={backToCurrent}
                    />
                    <button
                        type="button"
                        disabled={busy || archived || dataLoading}
                        onClick={() => {
                            void prepare(
                                editor.fields.action,
                                editor.fields,
                                editor.current.version,
                            );
                        }}
                    >
                        {t("保留输入并重新读取资料", "Keep inputs and reload references")}
                    </button>
                    <button type="button" disabled={busy} onClick={backToCurrent}>
                        {t("放弃编辑并读取当前版本", "Discard edits and read current version")}
                    </button>
                </>
            ) : (
                <>
                    {current && <BusinessDocumentCurrentFacts current={current} locale={locale} />}
                    {!current && !error && (
                        <p role="status">
                            {t("正在读取当前单据…", "Loading the current document…")}
                        </p>
                    )}
                    {current?.status === "active" && (
                        <div className="form-actions">
                            <button
                                type="button"
                                disabled={busy || archived || dataLoading}
                                onClick={() => {
                                    void prepare("correct");
                                }}
                            >
                                {t("更正整张单据", "Correct whole document")}
                            </button>
                            <button
                                type="button"
                                disabled={busy || archived || dataLoading}
                                onClick={() => {
                                    void prepare("cancel");
                                }}
                            >
                                {t("取消整张单据", "Cancel whole document")}
                            </button>
                        </div>
                    )}
                    <div className="form-actions">
                        <button type="button" disabled={busy} onClick={backToCurrent}>
                            {t("重新读取当前版本", "Reload current version")}
                        </button>
                        <button type="button" disabled={busy} onClick={onBack}>
                            {t("返回单据列表", "Back to documents")}
                        </button>
                    </div>
                </>
            )}
            {archived && (
                <p>
                    {t(
                        "主体已归档，仍可读取历史事实；不能提交新的修订。",
                        "This entity is archived. Historical facts remain readable; new revisions are unavailable.",
                    )}
                </p>
            )}
            {busy && (
                <p role="status">{t("正在准备完整审阅…", "Preparing the complete review…")}</p>
            )}
            {error != null && <p role="alert">{documentRevisionError(error, locale)}</p>}
        </section>
    );
}
