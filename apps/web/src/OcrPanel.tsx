import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import { downloadFile, listFiles } from "./files-api";
import type { StoredFile } from "./files-api";
import type { Locale } from "./i18n";
import { ocrError } from "./ocr-errors";
import { OcrDestination } from "./OcrDestination";
import type { OcrCopyController } from "./ocr-copy";
import type { PendingConfirmationController } from "./pending-confirmations";
import type { Entity, Account, Asset, Category } from "./ledger-api";
import {
    getDraftReview,
    getConfirmation,
    getOcrDraft,
    listOcrDrafts,
    listOcrJobs,
    retryOcrJob,
    saveDraftReview,
} from "./ocr-api";
import type { DraftDetail, OcrDraft, OcrJob, ReviewView } from "./ocr-api";
import type { ConfirmationReceipt } from "./ocr-api";
import type { OcrJobIntents } from "./ocr-job-intents";
import "./ocr.css";
import { OriginalPreview } from "./OriginalPreview";

function fieldName(path: string, locale: Locale): string {
    const role = path.split(".").at(-1) ?? "";
    const names: Record<string, [string, string]> = {
        date: ["日期", "Date"],
        document_date: ["单据日期", "Document date"],
        currency: ["币种", "Currency"],
        amount: ["金额", "Amount"],
        total: ["合计", "Total"],
        subtotal: ["小计", "Subtotal"],
        tax: ["税额", "Tax"],
        opening_balance: ["期初余额", "Opening balance"],
        closing_balance: ["期末余额", "Closing balance"],
    };
    const match = /^(.*)_(usd|gbp|eur|hkd|cny)$/u.exec(role);
    const name = names[match?.[1] ?? role]?.[locale === "zh" ? 0 : 1] ?? role.replaceAll("_", " ");
    return match ? `${name} (${match[2].toUpperCase()})` : name;
}
type Props = {
    session: Session;
    locale: Locale;
    entity: Entity;
    entities: Entity[];
    copies: OcrCopyController;
    jobs: OcrJobIntents;
    controller: PendingConfirmationController;
    accounts: Account[];
    assets: Asset[];
    categories: Category[];
    dataLoading: boolean;
    onUnauthorized: () => void;
    onEditingChange: (editing: boolean) => void;
};
export function OcrPanel({
    session,
    locale,
    entity,
    entities,
    copies,
    jobs: intents,
    controller,
    accounts,
    assets,
    categories,
    dataLoading,
    onUnauthorized,
    onEditingChange,
}: Props) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const ledger = entity.ledger.id;
    const copy = useSyncExternalStore(copies.subscribe, copies.getSnapshot);
    const copyLocked = copy.context !== null;
    const confirmation = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
    const confirmationLocked = !["idle", "rejected", "confirmed"].includes(confirmation.status);
    const [formEditing, setFormEditing] = useState(false);
    const active = useRef(true);
    const unauthorized = useRef(onUnauthorized);
    useEffect(() => {
        unauthorized.current = onUnauthorized;
    }, [onUnauthorized]);
    useEffect(() => {
        active.current = true;
        return () => {
            active.current = false;
        };
    }, []);
    const [files, setFiles] = useState<StoredFile[]>([]),
        [jobs, setJobs] = useState<OcrJob[]>([]);
    const [drafts, setDrafts] = useState<OcrDraft[]>([]),
        [fileId, setFileId] = useState("");
    const [jobId, setJobId] = useState(""),
        [draftId, setDraftId] = useState(() =>
            copy.context?.ledger === ledger ? copy.context.draft : "",
        );
    const [page, setPage] = useState(0),
        [filePage, setFilePage] = useState(0),
        [jobPage, setJobPage] = useState(0);
    const [refresh, setRefresh] = useState(0),
        [busy, setBusy] = useState(false);
    const [detail, setDetail] = useState<DraftDetail | null>(null),
        [review, setReview] = useState<ReviewView | null>(null);
    const [confirmed, setConfirmed] = useState<string[]>([]),
        [dirty, setDirty] = useState(false);
    const [error, setError] = useState<unknown>(null);
    const [receipt, setReceipt] = useState<ConfirmationReceipt | null>(null);
    const [detailRefresh, setDetailRefresh] = useState(0);
    function selectJob(id: string) {
        setJobId(id);
        setDraftId("");
        setDrafts([]);
        setPage(0);
        setRefresh((v) => v + 1);
    }
    function failed(problem: unknown) {
        if (!active.current) return;
        if (problem instanceof ApiError && problem.kind === "unauthorized") unauthorized.current();
        else setError(problem);
    }
    useEffect(() => {
        onEditingChange(dirty || busy || formEditing);
        return () => onEditingChange(false);
    }, [dirty, busy, formEditing, onEditingChange]);
    useEffect(() => {
        const abort = new AbortController();
        void Promise.all([
            listFiles(
                ledger,
                { limit: 25, offset: filePage * 25, include_archived: false },
                abort.signal,
            ),
            listOcrJobs(ledger, { limit: 25, offset: jobPage * 25 }, abort.signal),
            listOcrDrafts(
                ledger,
                { limit: 25, offset: page * 25, job_id: jobId || undefined },
                abort.signal,
            ),
        ])
            .then(([nextFiles, nextJobs, nextDrafts]) => {
                if (abort.signal.aborted) return;
                setFiles(nextFiles);
                setJobs(nextJobs);
                setDrafts(nextDrafts);
            })
            .catch((problem) => {
                if (abort.signal.aborted) return;
                if (problem instanceof ApiError && problem.kind === "unauthorized")
                    unauthorized.current();
                else setError(problem);
            });
        return () => abort.abort();
    }, [ledger, filePage, jobPage, page, jobId, refresh]);
    useEffect(() => {
        if (!jobs.some((job) => job.state === "pending" || job.state === "running")) return;
        const timer = window.setInterval(() => setRefresh((value) => value + 1), 5000);
        return () => window.clearInterval(timer);
    }, [jobs]);
    useEffect(() => {
        setDetail(null);
        setReview(null);
        setReceipt(null);
        setConfirmed([]);
        setDirty(false);
        if (!draftId) return;
        const abort = new AbortController();
        void Promise.all([
            getOcrDraft(ledger, draftId, abort.signal),
            getDraftReview(ledger, draftId, abort.signal),
        ])
            .then(async ([nextDetail, nextReview]) => {
                const stored =
                    nextReview.status === "confirmed"
                        ? await getConfirmation(ledger, draftId, abort.signal)
                        : null;
                if (abort.signal.aborted) return;
                setReceipt(stored);
                setDetail(nextDetail);
                setReview(nextReview);
                setConfirmed(nextReview.review.confirmed ?? []);
            })
            .catch((problem) => {
                if (abort.signal.aborted) return;
                if (problem instanceof ApiError && problem.kind === "unauthorized")
                    unauthorized.current();
                else setError(problem);
            });
        return () => abort.abort();
    }, [ledger, draftId, detailRefresh]);
    useEffect(() => {
        if (
            review &&
            confirmation.receipts.some(
                (r) => r.draft_id === draftId && r.draft_version > review.version,
            )
        ) {
            setDetailRefresh((v) => v + 1);
            setRefresh((v) => v + 1);
        }
    }, [confirmation.receipts, draftId, review]);
    async function start() {
        setBusy(true);
        setError(null);
        try {
            const job = await intents.start(session, ledger, fileId);
            if (!active.current) return;
            selectJob(job.id);
        } catch (problem) {
            failed(problem);
        } finally {
            if (active.current) setBusy(false);
        }
    }
    async function retry(job: OcrJob) {
        setBusy(true);
        setError(null);
        try {
            await retryOcrJob(session.csrf_token, ledger, job.id, job.version);
            if (!active.current) return;
            setRefresh((v) => v + 1);
        } catch (problem) {
            failed(problem);
            if (active.current) setRefresh((v) => v + 1);
        } finally {
            if (active.current) setBusy(false);
        }
    }
    async function save(status: "draft" | "ignored") {
        if (!review) return;
        setBusy(true);
        setError(null);
        try {
            const saved = await saveDraftReview(session.csrf_token, ledger, draftId, {
                expected_version: review.version,
                status,
                review: { ...review.review, confirmed },
            });
            if (!active.current) return;
            setReview(saved);
            setDirty(false);
            setRefresh((v) => v + 1);
        } catch (problem) {
            failed(problem);
        } finally {
            if (active.current) setBusy(false);
        }
    }
    async function original() {
        if (!detail) return;
        setBusy(true);
        try {
            const blob = await downloadFile(ledger, detail.file_id);
            if (!active.current) return;
            const url = URL.createObjectURL(blob),
                link = document.createElement("a");
            link.href = url;
            link.download = `document.${blob.type === "application/pdf" ? "pdf" : blob.type.split("/")[1]}`;
            link.click();
            window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch (problem) {
            failed(problem);
        } finally {
            if (active.current) setBusy(false);
        }
    }
    const locked = busy || dirty || formEditing || confirmationLocked || copyLocked;
    const stateName = (state: string) =>
        ({
            pending: t("排队中", "Queued"),
            running: t("识别中", "Processing"),
            succeeded: t("已识别", "Processed"),
            failed: t("识别失败", "Failed"),
            draft: t("待复核", "Review needed"),
            ignored: t("已忽略", "Ignored"),
            confirmed: t("已确认", "Confirmed"),
        })[state] ?? state;
    const pager = (value: number, length: number, change: (next: number) => void) => (
        <div className="ocr-actions">
            <button disabled={locked || value === 0} onClick={() => change(value - 1)}>
                {t("上一页", "Previous")}
            </button>
            <span>{value + 1}</span>
            <button disabled={locked || length < 25} onClick={() => change(value + 1)}>
                {t("下一页", "Next")}
            </button>
        </div>
    );
    return (
        <section
            className="ocr-panel"
            aria-label={t("票据识别与复核", "Document recognition and review")}
        >
            <p>
                {t(
                    "识别只生成草稿，未经人工确认不会记账。OCR 候选须逐项核对。",
                    "Recognition creates drafts only. Nothing is posted without human confirmation. Review every OCR candidate.",
                )}
            </p>
            {error !== null && <p role="alert">{ocrError(error, locale)}</p>}
            <div className="field">
                <label htmlFor="ocr-file">{t("已上传原件", "Uploaded original")}</label>
                <select
                    id="ocr-file"
                    value={fileId}
                    disabled={locked}
                    onChange={(event) => setFileId(event.target.value)}
                >
                    <option value="">
                        {t(
                            "请选择原件（在票据与证件中上传）",
                            "Choose an original (upload in Documents)",
                        )}
                    </option>
                    {files.map((file) => (
                        <option key={file.id} value={file.id}>
                            {file.title || file.original_filename}
                        </option>
                    ))}
                </select>
            </div>
            {pager(filePage, files.length, (value) => {
                setFilePage(value);
                setFileId("");
            })}
            <div className="ocr-actions">
                <button
                    disabled={locked || !fileId || entity.archived}
                    onClick={() => void start()}
                >
                    {t("开始识别／重试原请求", "Start recognition / retry original request")}
                </button>
                <button
                    disabled={locked}
                    onClick={() => {
                        setError(null);
                        setRefresh((v) => v + 1);
                    }}
                >
                    {t("刷新状态", "Refresh status")}
                </button>
            </div>
            <h3>{t("识别任务", "Recognition tasks")}</h3>
            <button
                disabled={locked}
                onClick={() => {
                    selectJob("");
                }}
            >
                {t("全部草稿", "All drafts")}
            </button>
            <ul className="ocr-list">
                {jobs.map((job) => (
                    <li key={job.id}>
                        <button
                            disabled={locked}
                            aria-pressed={jobId === job.id}
                            data-job-id={job.id}
                            onClick={() => {
                                selectJob(job.id);
                            }}
                        >
                            {new Date(job.created_at).toLocaleString(locale)} ·{" "}
                            {stateName(job.state)}
                        </button>
                        {job.state === "failed" && (
                            <button
                                disabled={locked || entity.archived}
                                onClick={() => void retry(job)}
                            >
                                {t("重试识别", "Retry recognition")}
                            </button>
                        )}
                    </li>
                ))}
            </ul>
            {pager(jobPage, jobs.length, setJobPage)}
            <div className="ocr-columns">
                <div>
                    <h3>{t("逐行草稿", "Draft rows")}</h3>
                    <ul className="ocr-list">
                        {drafts.map((draft, index) => (
                            <li key={draft.id}>
                                <button
                                    disabled={locked}
                                    aria-pressed={draft.id === draftId}
                                    onClick={() => {
                                        setDraftId(draft.id);
                                        setError(null);
                                    }}
                                >
                                    {t("草稿", "Draft")} {page * 25 + index + 1} ·{" "}
                                    {stateName(draft.status)}
                                </button>
                            </li>
                        ))}
                    </ul>
                    {!drafts.length && (
                        <p>
                            {t(
                                "尚无草稿。任务完成后刷新。",
                                "No drafts yet. Refresh after processing completes.",
                            )}
                        </p>
                    )}
                    {pager(page, drafts.length, setPage)}
                </div>
                {detail && review && (
                    <article className="ocr-evidence">
                        <h3>
                            {t("原文与候选", "Source text and candidates")} ·{" "}
                            {stateName(review.status)}
                        </h3>
                        <button disabled={busy} onClick={() => void original()}>
                            {t("下载原件对照", "Download original for comparison")}
                        </button>
                        <OriginalPreview
                            key={detail.file_id}
                            ledger={ledger}
                            file={detail.file_id}
                            locale={locale}
                            onUnauthorized={onUnauthorized}
                        />
                        {Array.isArray(detail.evidence.pages) && (
                            <p>
                                {t("来源页：", "Source pages: ")}
                                {detail.evidence.pages
                                    .filter(
                                        (page) =>
                                            typeof page === "number" &&
                                            Number.isInteger(page) &&
                                            page >= 0,
                                    )
                                    .map((page) => (page as number) + 1)
                                    .join(", ")}
                            </p>
                        )}
                        <pre className="ocr-raw">
                            {typeof detail.recognition?.raw_text === "string"
                                ? detail.recognition.raw_text
                                : t(
                                      "无可提取文字，请对照原件手动填写。",
                                      "No extracted text. Refer to the original and enter values manually.",
                                  )}
                        </pre>
                        {review.fields.map((field) => (
                            <div className="ocr-field" key={field.path}>
                                <strong>{fieldName(field.path, locale)}</strong>
                                <span>
                                    {field.source === "text"
                                        ? t("文字层", "Text layer")
                                        : field.source === "ocr"
                                          ? "OCR"
                                          : t("手动", "Manual")}
                                </span>
                                <p>
                                    {t("候选", "Candidate")}:{" "}
                                    {field.candidate_value ?? t("缺失", "Missing")}
                                </p>
                                <p>
                                    {field.requires_confirmation
                                        ? t("需人工确认", "Human review required")
                                        : t("文字层可预填", "Text layer eligible for prefill")}
                                </p>
                                <label>
                                    <input
                                        type="checkbox"
                                        checked={confirmed.includes(field.path)}
                                        disabled={
                                            busy ||
                                            formEditing ||
                                            confirmationLocked ||
                                            copyLocked ||
                                            entity.archived ||
                                            review.status === "confirmed"
                                        }
                                        onChange={(event) => {
                                            setConfirmed((value) =>
                                                event.target.checked
                                                    ? [...value, field.path]
                                                    : value.filter((path) => path !== field.path),
                                            );
                                            setDirty(true);
                                        }}
                                    />
                                    {fieldName(field.path, locale)} · {t("已核对", "Reviewed")}
                                </label>
                            </div>
                        ))}
                        <div className="ocr-actions">
                            <button
                                disabled={
                                    busy ||
                                    formEditing ||
                                    confirmationLocked ||
                                    copyLocked ||
                                    entity.archived ||
                                    review.status === "confirmed"
                                }
                                onClick={() => void save("draft")}
                            >
                                {t("保存复核", "Save review")}
                            </button>
                            <button
                                disabled={
                                    busy ||
                                    formEditing ||
                                    confirmationLocked ||
                                    copyLocked ||
                                    entity.archived ||
                                    review.status === "confirmed"
                                }
                                onClick={() =>
                                    void save(review.status === "ignored" ? "draft" : "ignored")
                                }
                            >
                                {review.status === "ignored"
                                    ? t("恢复草稿", "Restore draft")
                                    : t("忽略草稿", "Ignore draft")}
                            </button>
                            <button
                                disabled={busy || formEditing || confirmationLocked}
                                onClick={() => {
                                    setError(null);
                                    setDetailRefresh((v) => v + 1);
                                }}
                            >
                                {t("放弃修改并重新加载", "Discard edits and reload")}
                            </button>
                        </div>
                        {receipt && (
                            <p>
                                {t("确认时的入账回执", "Original confirmation receipt")}:{" "}
                                {receipt.operation.id} ·{" "}
                                {"amount" in receipt.operation
                                    ? receipt.operation.amount
                                    : receipt.operation.source_amount}{" "}
                                {"asset_id" in receipt.operation
                                    ? receipt.operation.asset_id
                                    : receipt.operation.source_asset_id}
                            </p>
                        )}
                        <OcrDestination
                            entities={entities}
                            copies={copies}
                            session={session}
                            locale={locale}
                            ledger={ledger}
                            file={detail.file_id}
                            review={review}
                            confirmed={confirmed}
                            accounts={accounts}
                            assets={assets}
                            categories={categories}
                            controller={controller}
                            locked={busy || confirmationLocked || dataLoading || entity.archived}
                            dirty={dirty}
                            onEditing={setFormEditing}
                            onSaved={(next) => {
                                setReview(next);
                                setDirty(false);
                                setRefresh((v) => v + 1);
                            }}
                            onError={failed}
                        />
                    </article>
                )}
            </div>
        </section>
    );
}
