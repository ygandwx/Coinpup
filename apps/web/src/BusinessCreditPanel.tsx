import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import type { Asset, Category } from "./ledger-api";
import type { Locale } from "./i18n";
import { getBusinessConfirmation, listBusinessConfirmations } from "./business-confirmation-api";
import type { BusinessConfirmedSummary } from "./business-confirmation-api";
import { getCredit, getCreditCapacity, listCredits } from "./credit-api";
import type { CreditCreate, CreditDetail, CreditSummary } from "./credit-api";
import type { CreditReview } from "./credit-intent";
import { isCreditRevisionPlan } from "./credit-revision-intent";
import type { PendingBusinessCreditController } from "./pending-business-credit";
import { BusinessCreditForm } from "./BusinessCreditForm";
import { BusinessCreditResult } from "./BusinessCreditResult";
import { creditError } from "./credit-errors";
import { localDraftDate } from "./draft-fields";
import { formatAmount } from "./money";
import "./credit-form.css";

export function BusinessCreditPanel({
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
    onSaved,
}: {
    ledgerId: string;
    session: Session;
    locale: Locale;
    controller: PendingBusinessCreditController;
    assets: Asset[];
    categories: Category[];
    archived: boolean;
    dataLoading: boolean;
    onEditing: (value: boolean) => void;
    onUnauthorized: () => void;
    onSaved: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const pending = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
    const plan = pending.plan?.ledgerId === ledgerId ? pending.plan : null;
    const [mode, setMode] = useState<"sources" | "history">("sources");
    const [offset, setOffset] = useState(0),
        [refresh, setRefresh] = useState(0);
    const [target, setTarget] = useState<string | null>(null);
    const [editor, setEditor] = useState<{ body: CreditCreate; review: CreditReview } | null>(null);
    const [loaded, setLoaded] = useState<{
        key: string;
        rows?: BusinessConfirmedSummary[] | CreditSummary[];
        detail?: CreditDetail;
    } | null>(null);
    const [failure, setFailure] = useState<{ key: string; error: unknown } | null>(null);
    const [error, setError] = useState<unknown>(null);
    const [sourceRead, setSourceRead] = useState<string | null>(null);
    const callbacks = useRef({ onEditing, onUnauthorized, onSaved, assets });
    callbacks.current = { onEditing, onUnauthorized, onSaved, assets };
    const key = `${ledgerId}/${mode}/${target ?? offset}`;
    const open = !!target || !!plan;
    useEffect(() => {
        callbacks.current.onEditing(open);
        return () => callbacks.current.onEditing(false);
    }, [open]);
    useEffect(() => {
        if (pending.status === "auth-required") callbacks.current.onUnauthorized();
    }, [pending.status]);
    useEffect(() => {
        if (plan || (mode === "sources" && target)) return;
        const abort = new AbortController();
        async function read() {
            if (target) {
                const detail = await getCredit(ledgerId, target, abort.signal);
                if (
                    detail.ledger_id !== ledgerId ||
                    detail.id !== target ||
                    detail.receipt.ledger_id !== ledgerId ||
                    detail.receipt.id !== target ||
                    detail.receipt.document_id !== detail.document_id ||
                    detail.receipt.asset_id !== detail.asset_id ||
                    detail.receipt.source_version !== detail.source_version ||
                    detail.receipt.credit_date !== detail.credit_date
                )
                    throw new ApiError("server", 200, "invalid_response");
                if (!abort.signal.aborted) setLoaded({ key, detail });
            } else {
                const rows =
                    mode === "sources"
                        ? await listBusinessConfirmations(ledgerId, offset, abort.signal)
                        : await listCredits(ledgerId, undefined, offset, abort.signal);
                if (rows.some((row) => row.ledger_id !== ledgerId))
                    throw new ApiError("server", 200, "invalid_response");
                if (!abort.signal.aborted) setLoaded({ key, rows });
            }
            if (!abort.signal.aborted) setFailure(null);
        }
        read().catch((caught) => {
            if (abort.signal.aborted) return;
            if (caught instanceof ApiError && caught.status === 401)
                callbacks.current.onUnauthorized();
            else setFailure({ key, error: caught });
        });
        return () => abort.abort();
    }, [ledgerId, mode, offset, target, key, refresh, plan]);
    const needsSource = mode === "sources" && !!target && !plan && !dataLoading;
    useEffect(() => {
        if (!needsSource || !target) return;
        const abort = new AbortController();
        async function read() {
            const [capacity, detail] = await Promise.all([
                getCreditCapacity(ledgerId, target!, 0, abort.signal),
                getBusinessConfirmation(ledgerId, target!, abort.signal),
            ]);
            if (abort.signal.aborted) return;
            const asset = callbacks.current.assets.find(
                (row) => row.asset_id === capacity.asset_id,
            );
            if (
                !asset ||
                capacity.id !== target ||
                capacity.ledger_id !== ledgerId ||
                detail.document.id !== target ||
                detail.document.ledger_id !== ledgerId ||
                detail.document.asset_id !== capacity.asset_id ||
                detail.confirmation.document_id !== target ||
                detail.confirmation.ledger_id !== ledgerId ||
                detail.confirmation.document_version !== capacity.expected_version ||
                detail.confirmation.postings.length !== capacity.lines.length
            )
                throw new ApiError("server", 200, "invalid_response");
            const review = { capacity, confirmation: detail.confirmation, asset };
            setEditor((current) => ({
                body:
                    current?.body.document_id === target
                        ? current.body
                        : {
                              id: crypto.randomUUID(),
                              document_id: capacity.id,
                              expected_version: capacity.expected_version,
                              credit_date:
                                  localDraftDate() < capacity.issue_date
                                      ? capacity.issue_date
                                      : localDraftDate(),
                              reason: "",
                              lines: [],
                          },
                review,
            }));
            setSourceRead(`${key}/${refresh}`);
            setFailure(null);
        }
        read().catch((caught) => {
            if (abort.signal.aborted) return;
            if (caught instanceof ApiError && caught.status === 401)
                callbacks.current.onUnauthorized();
            else setFailure({ key, error: caught });
        });
        return () => abort.abort();
    }, [ledgerId, target, key, needsSource, refresh]);
    function fail(caught: unknown) {
        if (caught instanceof ApiError && caught.status === 401) callbacks.current.onUnauthorized();
        else setError(caught);
    }
    function close() {
        const saved = controller.getSnapshot().status === "confirmed";
        if (!controller.dismiss()) return;
        setTarget(null);
        setEditor(null);
        setError(null);
        setFailure(null);
        setRefresh((value) => value + 1);
        if (saved) {
            setMode("history");
            setOffset(0);
            callbacks.current.onSaved();
        }
    }
    function editRejected() {
        const snapshot = controller.getSnapshot();
        if (snapshot.status !== "rejected" || !snapshot.plan || isCreditRevisionPlan(snapshot.plan))
            return;
        const body = JSON.parse(snapshot.plan.bodyJson) as CreditCreate;
        const review = JSON.parse(snapshot.plan.reviewedJson) as CreditReview;
        if (!controller.dismiss()) return;
        setMode("sources");
        setTarget(body.document_id);
        setEditor({ body, review });
        setError(null);
        setRefresh((value) => value + 1);
    }
    const data = loaded?.key === key ? loaded : null;
    const currentEditor = editor?.body.document_id === target ? editor : null;
    return (
        <section className="credit-form" aria-label={t("贷项", "Credits")}>
            <h2>{t("贷项", "Credits")}</h2>
            {plan ? (
                !isCreditRevisionPlan(plan) && (
                    <BusinessCreditResult
                        pending={pending}
                        locale={locale}
                        onRetry={() => {
                            void controller.retry(session).catch(fail);
                        }}
                        onEdit={editRejected}
                        onClose={close}
                    />
                )
            ) : (
                <>
                    {!target && (
                        <div className="form-actions">
                            <button
                                type="button"
                                aria-pressed={mode === "sources"}
                                onClick={() => {
                                    setMode("sources");
                                    setOffset(0);
                                    setError(null);
                                }}
                            >
                                {t("选择原单据", "Choose original document")}
                            </button>
                            <button
                                type="button"
                                aria-pressed={mode === "history"}
                                onClick={() => {
                                    setMode("history");
                                    setOffset(0);
                                    setError(null);
                                }}
                            >
                                {t("贷项历史", "Credit history")}
                            </button>
                        </div>
                    )}
                    {failure?.key === key && (
                        <p role="alert">{creditError(failure.error, locale)}</p>
                    )}
                    {mode === "sources" && target ? (
                        currentEditor ? (
                            <BusinessCreditForm
                                ledgerId={ledgerId}
                                locale={locale}
                                body={currentEditor.body}
                                review={currentEditor.review}
                                categories={categories}
                                disabled={
                                    archived ||
                                    dataLoading ||
                                    !currentEditor.review.asset.enabled ||
                                    sourceRead !== `${key}/${refresh}` ||
                                    failure?.key === key
                                }
                                onChange={(update) =>
                                    setEditor((value) =>
                                        value ? { ...value, body: update(value.body) } : value,
                                    )
                                }
                                onSubmit={(body, review) => {
                                    if (archived || dataLoading || controller.getSnapshot().plan)
                                        return;
                                    setError(null);
                                    void controller
                                        .start(session, ledgerId, body, review)
                                        .catch(fail);
                                }}
                                onCancel={close}
                            />
                        ) : (
                            <p role="status">
                                {t(
                                    "读取原行与可贷额度…",
                                    "Loading original lines and credit capacity…",
                                )}
                            </p>
                        )
                    ) : data?.detail ? (
                        <>
                            <p>
                                {data.detail.credit_date} · {data.detail.reason}
                            </p>
                            <p>
                                {t("原单据标识", "Original document ID")}: {data.detail.document_id}
                            </p>
                            <ol className="confirmation-lines">
                                {data.detail.receipt.postings.map((line) => (
                                    <li key={line.line_no}>
                                        <p>
                                            {t("净额", "Net")}:{" "}
                                            {formatAmount(line.net_amount, locale)}{" "}
                                            {data.detail!.asset_id}
                                        </p>
                                        <p>
                                            {t("税额", "Tax")}:{" "}
                                            {formatAmount(line.tax_amount, locale)}{" "}
                                            {data.detail!.asset_id}
                                        </p>
                                        <p>
                                            {t("含税额", "Gross")}:{" "}
                                            {formatAmount(line.total_amount, locale)}{" "}
                                            {data.detail!.asset_id}
                                        </p>
                                        <p>
                                            {t("归属日", "Recognition date")}:{" "}
                                            {line.recognition_date}
                                        </p>
                                        <details>
                                            <summary>
                                                {t(
                                                    "来源与回执标识",
                                                    "Source and receipt references",
                                                )}
                                            </summary>
                                            <p>
                                                {line.source_line_id} / {line.source_operation_id}
                                            </p>
                                            <p>
                                                {line.operation_id} / {line.journal_id}
                                            </p>
                                        </details>
                                    </li>
                                ))}
                            </ol>
                        </>
                    ) : data?.rows ? (
                        <>
                            <ul className="master-records">
                                {data.rows.map((row) => (
                                    <li key={row.id}>
                                        <strong>
                                            {"reason" in row
                                                ? row.reason
                                                : typeof row.party_snapshot.name === "string"
                                                  ? row.party_snapshot.name
                                                  : row.id}
                                        </strong>
                                        <span>
                                            {"credit_date" in row
                                                ? row.credit_date
                                                : row.issue_date}{" "}
                                            · {row.asset_id}
                                        </span>
                                        <button
                                            type="button"
                                            disabled={
                                                mode === "sources" && (archived || dataLoading)
                                            }
                                            onClick={() => {
                                                setEditor(null);
                                                setTarget(row.id);
                                                setError(null);
                                            }}
                                        >
                                            {mode === "sources"
                                                ? t("登记此单据贷项", "Credit this document")
                                                : t("查看贷项详情", "View credit details")}
                                        </button>
                                    </li>
                                ))}
                            </ul>
                            {data.rows.length === 0 && (
                                <p>{t("此页暂无记录。", "No records on this page.")}</p>
                            )}
                            <div className="form-actions">
                                <button
                                    type="button"
                                    disabled={offset === 0}
                                    onClick={() => setOffset((value) => value - 25)}
                                >
                                    {t("上一页", "Previous page")}
                                </button>
                                <button
                                    type="button"
                                    disabled={data.rows.length !== 25 || offset >= 100000}
                                    onClick={() => setOffset((value) => value + 25)}
                                >
                                    {t("下一页", "Next page")}
                                </button>
                            </div>
                        </>
                    ) : (
                        failure?.key !== key && <p role="status">{t("读取中…", "Loading…")}</p>
                    )}
                    <div className="form-actions">
                        <button type="button" onClick={() => setRefresh((value) => value + 1)}>
                            {t("重新读取", "Reload")}
                        </button>
                        {target && (
                            <button type="button" onClick={close}>
                                {t("返回贷项列表", "Back to credit list")}
                            </button>
                        )}
                    </div>
                </>
            )}
            {error !== null && <p role="alert">{creditError(error, locale)}</p>}
        </section>
    );
}
