import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import type { Asset, Category } from "./ledger-api";
import type { Locale } from "./i18n";
import type { CreditCurrent } from "./credit-api";
import { getCreditCurrent } from "./credit-api";
import type { PendingBusinessCreditController } from "./pending-business-credit";
import { isCreditRevisionPlan } from "./credit-revision-intent";
import type { CreditRevisionAction, CreditRevisionReview } from "./credit-revision-intent";
import type { CreditRevisionDraft } from "./credit-revision-fields";
import type { CreditRevisionEditor } from "./credit-revision-review-loader";
import { checkedCreditCurrent, loadCreditRevisionEditor } from "./credit-revision-review-loader";
import { BusinessCreditRevisionForm } from "./BusinessCreditRevisionForm";
import { BusinessCreditRevisionResult } from "./BusinessCreditRevisionResult";
import { BusinessCreditRevisionHistoryPanel } from "./BusinessCreditRevisionHistoryPanel";
import { creditRevisionError } from "./credit-revision-errors";

export function BusinessCreditRevisionPanel({
    ledgerId,
    creditId,
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
    onSaved,
}: {
    ledgerId: string;
    creditId: string;
    session: Session;
    locale: Locale;
    controller: PendingBusinessCreditController;
    assets: Asset[];
    categories: Category[];
    archived: boolean;
    dataLoading: boolean;
    onUnauthorized: () => void;
    onKeepTarget: () => void;
    onBack: () => void;
    onSaved: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const pending = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
    const plan =
        isCreditRevisionPlan(pending.plan) &&
        pending.plan.ledgerId === ledgerId &&
        pending.plan.creditId === creditId
            ? pending.plan
            : null;
    const scope = `${session.user.id}/${ledgerId}/${creditId}`;
    const [refresh, setRefresh] = useState(0);
    const readKey = `${scope}/${refresh}`;
    const request = useMemo(() => ({ scope, session, readKey }), [scope, session, readKey]);
    const [loaded, setLoaded] = useState<{
        request: typeof request;
        current: CreditCurrent;
    } | null>(null);
    const [failure, setFailure] = useState<{ request: typeof request; error: unknown } | null>(
        null,
    );
    const [editor, setEditor] = useState<CreditRevisionEditor | null>(null);
    const [history, setHistory] = useState<{ documentId: string; assetId: string } | null>(null);
    const [busy, setBusy] = useState(false);
    const [readyRequest, setReadyRequest] = useState<typeof request | null>(null);
    const sourceReady = readyRequest === request;
    const sourceAbort = useRef<AbortController | null>(null),
        generation = useRef(0),
        active = useRef(false);
    const latest = useRef({ request, assets, onUnauthorized });
    latest.current = { request, assets, onUnauthorized };
    useEffect(() => {
        active.current = true;
        setBusy(false);
        setReadyRequest(null);
        return () => {
            active.current = false;
            generation.current += 1;
            sourceAbort.current?.abort();
        };
    }, [request]);
    useEffect(() => {
        if (pending.plan || pending.status === "auth-required" || editor || history) return;
        const abort = new AbortController();
        getCreditCurrent(ledgerId, creditId, abort.signal)
            .then((value) => {
                const current = checkedCreditCurrent(value, ledgerId, creditId);
                if (!abort.signal.aborted && latest.current.request === request) {
                    setLoaded({ request, current });
                    setFailure(null);
                }
            })
            .catch((error) => {
                if (abort.signal.aborted || latest.current.request !== request) return;
                if (error instanceof ApiError && error.status === 401)
                    latest.current.onUnauthorized();
                else setFailure({ request, error });
            });
        return () => abort.abort();
    }, [ledgerId, creditId, request, pending.plan, pending.status, editor, history]);
    function fail(error: unknown) {
        if (!active.current || latest.current.request !== request) return;
        if (error instanceof ApiError && error.status === 401) latest.current.onUnauthorized();
        else setFailure({ request, error });
    }
    async function prepare(action: CreditRevisionAction, preserve?: CreditRevisionDraft) {
        if (controller.getSnapshot().plan || archived || dataLoading) return;
        const token = ++generation.current;
        sourceAbort.current?.abort();
        const abort = new AbortController();
        sourceAbort.current = abort;
        setBusy(true);
        setReadyRequest(null);
        setFailure(null);
        try {
            const value = await loadCreditRevisionEditor(
                ledgerId,
                creditId,
                action,
                latest.current.assets,
                abort.signal,
                preserve?.body.expected_version,
            );
            if (
                abort.signal.aborted ||
                token !== generation.current ||
                latest.current.request !== request
            )
                return;
            setEditor({ ...value, draft: preserve ?? value.draft });
            setReadyRequest(request);
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
        setFailure(null);
        setReadyRequest(null);
        setRefresh((value) => value + 1);
    }
    function editRejected() {
        const saved = controller.getSnapshot();
        if (
            saved.status !== "rejected" ||
            !isCreditRevisionPlan(saved.plan) ||
            saved.plan.ledgerId !== ledgerId ||
            saved.plan.creditId !== creditId
        )
            return;
        const draft: CreditRevisionDraft =
            saved.plan.action === "correct"
                ? { action: "correct", body: JSON.parse(saved.plan.bodyJson) }
                : { action: "cancel", body: JSON.parse(saved.plan.bodyJson) };
        const review = JSON.parse(saved.plan.reviewedJson) as CreditRevisionReview;
        onKeepTarget();
        if (!controller.dismiss()) return;
        setEditor({ draft, review });
        void prepare(draft.action, draft);
    }
    const current = loaded?.request === request ? loaded.current : null;
    const error = failure?.request === request ? failure.error : null;
    if (plan)
        return (
            <BusinessCreditRevisionResult
                plan={plan}
                status={pending.status}
                revisionReceipt={pending.revisionReceipt}
                error={pending.error}
                locale={locale}
                onRetry={() => {
                    void controller.retry(session).catch(fail);
                }}
                onEdit={editRejected}
                onClose={() => {
                    const saved = controller.getSnapshot().status === "confirmed";
                    if (!controller.dismiss()) return;
                    if (saved) onSaved();
                    onBack();
                }}
            />
        );
    if (pending.plan || pending.status === "auth-required") return null;
    return (
        <section
            className="credit-form"
            aria-label={t("贷项当前状态与修订", "Credit current state and revisions")}
        >
            {editor ? (
                <>
                    <BusinessCreditRevisionForm
                        ledgerId={ledgerId}
                        creditId={creditId}
                        locale={locale}
                        draft={editor.draft}
                        review={editor.review}
                        categories={categories}
                        disabled={archived || dataLoading || busy || !sourceReady}
                        onChange={(update) =>
                            setEditor((value) =>
                                value ? { ...value, draft: update(value.draft) } : value,
                            )
                        }
                        onSubmit={(draft, review) => {
                            if (
                                archived ||
                                dataLoading ||
                                busy ||
                                !sourceReady ||
                                controller.getSnapshot().plan
                            )
                                return;
                            void controller
                                .startRevision(
                                    session,
                                    ledgerId,
                                    creditId,
                                    draft.action,
                                    draft.body,
                                    review,
                                )
                                .catch(fail);
                        }}
                        onBack={backToCurrent}
                    />
                    <button
                        type="button"
                        disabled={busy || archived || dataLoading}
                        onClick={() => {
                            void prepare(editor.draft.action, editor.draft);
                        }}
                    >
                        {t("重新读取来源，保留输入", "Reload sources, keep input")}
                    </button>
                    <button type="button" onClick={backToCurrent}>
                        {t("放弃编辑，返回当前状态", "Discard edits and return to current state")}
                    </button>
                </>
            ) : history ? (
                <>
                    <BusinessCreditRevisionHistoryPanel
                        ledgerId={ledgerId}
                        creditId={creditId}
                        documentId={history.documentId}
                        assetId={history.assetId}
                        session={session}
                        locale={locale}
                        available
                        onUnauthorized={onUnauthorized}
                    />
                    <button
                        type="button"
                        onClick={() => {
                            setHistory(null);
                            setRefresh((value) => value + 1);
                        }}
                    >
                        {t("返回当前状态", "Back to current state")}
                    </button>
                </>
            ) : (
                <>
                    {current ? (
                        <>
                            <h3>{t("当前贷项组", "Current credit group")}</h3>
                            <p>
                                {t("组版本", "Group version")}: {current.version} ·{" "}
                                {current.status === "active"
                                    ? t("有效", "Active")
                                    : t("已取消，不能恢复", "Cancelled; cannot be reactivated")}
                            </p>
                            <p>
                                {current.credit_date} {current.credit_reason}
                            </p>
                            <p>
                                {t("当前有效成员", "Current active members")}:{" "}
                                {current.members.length}
                            </p>
                            <p className="help-text">
                                {t(
                                    "当前状态不是任何未知请求的成功证明；原创建详情和回执保持不变。",
                                    "Current state cannot prove the outcome of an unknown request. Original creation details and receipts remain unchanged.",
                                )}
                            </p>
                            <button
                                type="button"
                                disabled={busy}
                                onClick={() => {
                                    setHistory({
                                        documentId: current.document_id,
                                        assetId: current.asset_id,
                                    });
                                    setFailure(null);
                                }}
                            >
                                {t("查看版本历史", "View version history")}
                            </button>
                            {current.status === "active" && (
                                <div className="form-actions">
                                    <button
                                        type="button"
                                        disabled={
                                            archived ||
                                            dataLoading ||
                                            busy ||
                                            current.version >= 2147483647 ||
                                            !assets.find(
                                                (item) => item.asset_id === current.asset_id,
                                            )?.enabled
                                        }
                                        onClick={() => {
                                            void prepare("correct");
                                        }}
                                    >
                                        {t("更正完整贷项组", "Correct complete credit group")}
                                    </button>
                                    <button
                                        type="button"
                                        disabled={
                                            archived ||
                                            dataLoading ||
                                            busy ||
                                            current.version >= 2147483647
                                        }
                                        onClick={() => {
                                            void prepare("cancel");
                                        }}
                                    >
                                        {t("取消完整贷项组", "Cancel complete credit group")}
                                    </button>
                                </div>
                            )}
                        </>
                    ) : (
                        error === null && (
                            <p role="status">
                                {t("读取当前贷项组…", "Loading the current credit group…")}
                            </p>
                        )
                    )}
                    <div className="form-actions">
                        <button
                            type="button"
                            disabled={busy}
                            onClick={() => setRefresh((value) => value + 1)}
                        >
                            {t("重新读取当前状态", "Reload current state")}
                        </button>
                        <button type="button" disabled={busy} onClick={onBack}>
                            {t("返回原创建详情", "Back to original creation details")}
                        </button>
                    </div>
                </>
            )}
            {busy && <p role="status">{t("读取完整来源，请稍候…", "Loading complete sources…")}</p>}
            {error !== null && <p role="alert">{creditRevisionError(error, locale)}</p>}
        </section>
    );
}
