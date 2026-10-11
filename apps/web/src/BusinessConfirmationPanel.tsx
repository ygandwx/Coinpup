import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import type { Locale } from "./i18n";
import type { Asset } from "./ledger-api";
import { businessError } from "./business-errors";
import { getBusinessDraft, listBusinessDrafts } from "./business-draft-api";
import type { BusinessDraft, BusinessDraftSummary } from "./business-draft-api";
import { getBusinessConfirmation, listBusinessConfirmations } from "./business-confirmation-api";
import type {
    BusinessConfirmationDetail,
    BusinessConfirmedSummary,
} from "./business-confirmation-api";
import type { PendingBusinessConfirmationController } from "./pending-business-confirmation";
import { formatAmount, parseAmount } from "./money";

type Document = BusinessDraft | BusinessConfirmationDetail["document"];
export function BusinessConfirmationPanel({
    ledgerId,
    session,
    locale,
    controller,
    assets,
    archived,
    dataLoading,
    onEditing,
    onUnauthorized,
}: {
    ledgerId: string;
    session: Session;
    locale: Locale;
    controller: PendingBusinessConfirmationController;
    assets: Asset[];
    archived: boolean;
    dataLoading: boolean;
    onEditing: (value: boolean) => void;
    onUnauthorized: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const pending = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
    const plan = pending.plan?.ledgerId === ledgerId ? pending.plan : null;
    const [mode, setMode] = useState<"draft" | "confirmed">("draft");
    const [offset, setOffset] = useState(0),
        [refresh, setRefresh] = useState(0);
    const [target, setTarget] = useState<string | null>(null);
    const [selected, setSelected] = useState<Document | null>(null);
    const [sealed, setSealed] = useState<BusinessConfirmationDetail["confirmation"] | null>(null);
    const [reviewed, setReviewed] = useState(false);
    const [error, setError] = useState<unknown>(null);
    const [result, setResult] = useState<{
        key: string;
        rows: (BusinessDraftSummary | BusinessConfirmedSummary)[];
    } | null>(null);
    const [failure, setFailure] = useState<{ key: string; error: unknown } | null>(null);
    const callbacks = useRef({ onEditing, onUnauthorized });
    callbacks.current = { onEditing, onUnauthorized };
    const detailAbort = useRef<AbortController | null>(null);
    const key = JSON.stringify([ledgerId, mode, offset, refresh]);
    const rows = result?.key === key ? result.rows : null;
    const readError = failure?.key === key ? failure.error : null;
    const document: Document | null = plan ? JSON.parse(plan.reviewedJson) : selected;
    const receipt = plan ? pending.receipt : sealed;
    const open = !!target || !!plan;
    const unresolved = !["idle", "confirmed", "rejected"].includes(pending.status);
    const asset = assets.find((item) => item.asset_id === document?.asset_id);
    const eligible = (() => {
        try {
            return (
                !!document &&
                !!asset?.enabled &&
                !document.archived &&
                document.lines.length > 0 &&
                parseAmount(document.total_amount, asset.scale) > 0n
            );
        } catch {
            return false;
        }
    })();
    useEffect(() => {
        callbacks.current.onEditing(open);
        return () => callbacks.current.onEditing(false);
    }, [open]);
    useEffect(() => {
        if (pending.status === "auth-required") callbacks.current.onUnauthorized();
    }, [pending.status]);
    useEffect(() => () => detailAbort.current?.abort(), []);
    useEffect(() => {
        const abort = new AbortController();
        const read = mode === "draft" ? listBusinessDrafts : listBusinessConfirmations;
        read(ledgerId, offset, abort.signal)
            .then((items) => {
                if (!abort.signal.aborted) setResult({ key, rows: items });
            })
            .catch((caught) => {
                if (abort.signal.aborted) return;
                if (caught instanceof ApiError && caught.status === 401)
                    callbacks.current.onUnauthorized();
                else setFailure({ key, error: caught });
            });
        return () => abort.abort();
    }, [ledgerId, mode, offset, key]);
    function fail(caught: unknown) {
        if (caught instanceof ApiError && caught.status === 401) callbacks.current.onUnauthorized();
        else setError(caught);
    }
    async function read(id: string) {
        if (open || !controller.dismiss()) return;
        detailAbort.current?.abort();
        const abort = new AbortController();
        detailAbort.current = abort;
        setTarget(id);
        setSelected(null);
        setSealed(null);
        setReviewed(false);
        setError(null);
        try {
            const value =
                mode === "draft"
                    ? await getBusinessDraft(ledgerId, id, abort.signal)
                    : await getBusinessConfirmation(ledgerId, id, abort.signal);
            if (abort.signal.aborted) return;
            const row = "document" in value ? value.document : value;
            if (row.id !== id || row.ledger_id !== ledgerId || row.state !== mode)
                throw new ApiError("server", 200, "invalid_response");
            setSelected(row);
            if ("confirmation" in value) setSealed(value.confirmation);
        } catch (caught) {
            if (!abort.signal.aborted) fail(caught);
        }
    }
    function close() {
        if (!controller.dismiss()) return;
        detailAbort.current?.abort();
        if (receipt) {
            setMode("confirmed");
            setOffset(0);
        }
        setTarget(null);
        setSelected(null);
        setSealed(null);
        setReviewed(false);
        setError(null);
        setRefresh((value) => value + 1);
    }
    async function confirm() {
        if (
            !reviewed ||
            !eligible ||
            !selected ||
            selected.state !== "draft" ||
            selected.archived ||
            !asset?.enabled ||
            archived ||
            dataLoading ||
            controller.getSnapshot().plan
        )
            return;
        try {
            await controller.start(session, selected, asset);
        } catch (caught) {
            fail(caught);
        }
    }
    async function retry() {
        if (controller.getSnapshot().status !== "unknown") return;
        try {
            await controller.retry(session);
        } catch (caught) {
            fail(caught);
        }
    }
    const amount = (value: string) => `${formatAmount(value, locale)} ${document?.asset_id ?? ""}`;
    return (
        <section
            className="business-panel business-confirmation-panel"
            aria-label={t("单据确认", "Confirm documents")}
        >
            <h2>{t("单据确认", "Confirm documents")}</h2>
            <p className="help-text">
                {t(
                    "核对已保存版本后确认。含税总额计入收入或费用，税额另列；确认生成应收或应付，不发生资金收付。",
                    "Review the saved version before confirming. Income or expense includes tax, shown separately. Confirmation records receivables or payables without moving money.",
                )}
            </p>
            {archived && (
                <p role="status">
                    {t(
                        "主体已归档，仅可查看历史或重放原请求。",
                        "This entity is archived. History and original-request replay remain available.",
                    )}
                </p>
            )}
            {open ? (
                <>
                    {!document && !error && <p role="status">{t("正在读取…", "Loading…")}</p>}
                    {document && (
                        <>
                            <h3>
                                {document.document_kind === "invoice"
                                    ? t("销售 Invoice", "Sales invoice")
                                    : t("采购账单", "Purchase bill")}
                            </h3>
                            <p className="help-text">
                                {document.id} · {t("版本", "Version")}{" "}
                                {receipt?.document_version ?? document.version}
                            </p>
                            <dl className="confirmation-facts">
                                <dt>{t("开票主体", "Issuer")}</dt>
                                <dd>{String(document.issuer_snapshot.name ?? "—")}</dd>
                                <dt>{t("往来单位", "Business party")}</dt>
                                <dd>{String(document.party_snapshot.name ?? "—")}</dd>
                                <dt>{t("开票日期 / 到期日期", "Issue date / Due date")}</dt>
                                <dd>
                                    {document.issue_date} / {document.due_date ?? "—"}
                                </dd>
                                <dt>{t("未税 / 税额 / 含税总额", "Net / Tax / Gross total")}</dt>
                                <dd data-testid="confirmation-totals">
                                    {amount(document.net_amount)} / {amount(document.tax_amount)} /{" "}
                                    {amount(document.total_amount)}
                                </dd>
                                <dt>{t("备注", "Notes")}</dt>
                                <dd>{document.notes || "—"}</dd>
                            </dl>
                            <ol className="master-records confirmation-lines">
                                {document.lines.map((line) => (
                                    <li key={line.id}>
                                        <strong>
                                            {line.line_no}. {line.description}
                                        </strong>
                                        <span>
                                            {t(
                                                "数量 × 单价；折扣；税率",
                                                "Quantity × unit price; discount; tax rate",
                                            )}
                                            : {line.quantity} × {line.unit_price};{" "}
                                            {line.discount_amount}; {line.tax_rate_percent}%
                                        </span>
                                        <span>
                                            {t("未税 / 税额 / 含税", "Net / Tax / Gross")}:{" "}
                                            {amount(line.net_amount)} / {amount(line.tax_amount)} /{" "}
                                            {amount(line.total_amount)}
                                        </span>
                                        <span>
                                            {t("归属日", "Recognition date")}:{" "}
                                            {line.recognition_date}
                                        </span>
                                        <span>
                                            {t("分类 / 项目", "Category / Project")}:{" "}
                                            {String(
                                                line.category_snapshot.name ?? line.category_id,
                                            )}{" "}
                                            / {String(line.project_snapshot?.name ?? "—")}
                                        </span>
                                        {receipt && (
                                            <span>
                                                {t("凭证", "Journal")}:{" "}
                                                {receipt.postings.find(
                                                    (member) => member.line_id === line.id,
                                                )?.journal_id ??
                                                    t(
                                                        "零金额行，不生成分录",
                                                        "Zero amount; no posting",
                                                    )}
                                            </span>
                                        )}
                                    </li>
                                ))}
                            </ol>
                            {!receipt && document.state === "draft" && !plan && (
                                <>
                                    {!eligible && !dataLoading && (
                                        <p role="status">
                                            {t(
                                                "确认需要未归档草稿、可用资产和正的含税总额。",
                                                "Confirmation requires an active draft, an enabled asset and a positive gross total.",
                                            )}
                                        </p>
                                    )}
                                    <label className="archive-toggle">
                                        <input
                                            type="checkbox"
                                            checked={reviewed}
                                            onChange={(event) => setReviewed(event.target.checked)}
                                            disabled={archived || !eligible || dataLoading}
                                        />
                                        {t(
                                            "已核对金额、日期、分类和保存的资料，确认后此版本将封存。",
                                            "I reviewed amounts, dates, categories and saved details. Confirmation will seal this version.",
                                        )}
                                    </label>
                                    <button
                                        className="primary-button"
                                        type="button"
                                        disabled={
                                            !reviewed ||
                                            archived ||
                                            !eligible ||
                                            dataLoading ||
                                            !asset?.enabled
                                        }
                                        onClick={confirm}
                                    >
                                        {t("确认并入账", "Confirm and post")}
                                    </button>
                                </>
                            )}
                            {receipt && (
                                <p role="status">
                                    {t(
                                        "已确认；原币金额与凭证映射已封存。",
                                        "Confirmed; original amounts and journal mappings are sealed.",
                                    )}
                                </p>
                            )}
                        </>
                    )}
                    {plan && pending.status === "submitting" && (
                        <p role="status">{t("正在确认…", "Confirming…")}</p>
                    )}
                    {plan && pending.status === "unknown" && (
                        <div role="alert">
                            <p>
                                {t(
                                    "确认结果未知，请重试原请求，不要创建重复单据。刷新或关闭页面会丢失内存意图。",
                                    "The confirmation result is unknown. Retry the original request; do not create a duplicate. Reloading or closing loses the in-memory intent.",
                                )}
                            </p>
                            <button type="button" onClick={retry}>
                                {t("重试原确认", "Retry original confirmation")}
                            </button>
                        </div>
                    )}
                    {plan && pending.status === "idempotency-conflict" && (
                        <p role="alert">
                            {t(
                                "原请求键发生冲突，无法安全创建新意图。请核对服务端记录。",
                                "The original request key conflicts. A new intent is blocked; review server records.",
                            )}
                        </p>
                    )}
                    {plan && pending.status === "rejected" && (
                        <p role="alert">{businessError(pending.error, locale)}</p>
                    )}
                    {error != null && <p role="alert">{businessError(error, locale)}</p>}
                    <button type="button" disabled={unresolved} onClick={close}>
                        {t("返回单据列表", "Back to documents")}
                    </button>
                </>
            ) : (
                <>
                    <div className="form-actions">
                        {(["draft", "confirmed"] as const).map((value) => (
                            <button
                                key={value}
                                type="button"
                                aria-pressed={mode === value}
                                onClick={() => {
                                    setMode(value);
                                    setOffset(0);
                                }}
                            >
                                {value === "draft"
                                    ? t("待确认草稿", "Drafts to confirm")
                                    : t("已确认单据", "Confirmed documents")}
                            </button>
                        ))}
                        <button type="button" onClick={() => setRefresh((value) => value + 1)}>
                            {t("刷新列表", "Refresh list")}
                        </button>
                    </div>
                    {readError != null && <p role="alert">{businessError(readError, locale)}</p>}
                    {!rows && !readError && <p role="status">{t("正在读取…", "Loading…")}</p>}
                    {rows && (
                        <ul className="master-records">
                            {rows.map((row) => (
                                <li key={row.id}>
                                    <strong>
                                        {row.document_kind === "invoice"
                                            ? t("销售 Invoice", "Sales invoice")
                                            : t("采购账单", "Purchase bill")}{" "}
                                        · {String(row.party_snapshot.name ?? row.party_id)}
                                    </strong>
                                    <span>
                                        {row.issue_date} · {t("版本", "Version")} {row.version}
                                        {row.archived ? t(" · 已归档", " · Archived") : ""}
                                    </span>
                                    <span>
                                        {formatAmount(row.total_amount, locale)} {row.asset_id}
                                    </span>
                                    <button type="button" onClick={() => read(row.id)}>
                                        {mode === "draft"
                                            ? t("核对已保存版本", "Review saved version")
                                            : t("查看封存单据", "View sealed document")}
                                    </button>
                                </li>
                            ))}
                        </ul>
                    )}
                    {rows?.length === 0 && (
                        <p>{t("此页暂无单据。", "No documents on this page.")}</p>
                    )}
                    <div className="form-actions">
                        <button
                            type="button"
                            disabled={!rows || offset === 0}
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
