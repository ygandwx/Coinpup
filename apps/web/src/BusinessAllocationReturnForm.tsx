import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { ApiError } from "./api";
import type { Locale } from "./i18n";
import type { Asset } from "./ledger-api";
import { getAllocationReturnCapacity } from "./allocation-return-api";
import type { AllocationReturnCapacity, AllocationReturnCreate } from "./allocation-return-api";
import type { AllocationReturnReview } from "./allocation-return-intent";
import { validReturnReview } from "./allocation-return-intent";
import { returnPaymentAmount } from "./allocation-return-fields";
import { formatAmount, parseAmount } from "./money";
import { settlementError } from "./settlement-errors";

export type ReturnEditor = {
    id: string;
    allocationId: string;
    paymentId: string;
    date: string;
    reason: string;
    amounts: Record<string, string>;
    capacity: AllocationReturnCapacity | null;
};

export function BusinessAllocationReturnForm({
    ledgerId,
    locale,
    assets,
    draft,
    disabled,
    onChange,
    onSubmit,
    onCancel,
    onUnauthorized,
}: {
    ledgerId: string;
    locale: Locale;
    assets: Asset[];
    draft: ReturnEditor;
    disabled: boolean;
    onChange: (update: (value: ReturnEditor) => ReturnEditor) => void;
    onSubmit: (body: AllocationReturnCreate, review: AllocationReturnReview) => Promise<void>;
    onCancel: () => void;
    onUnauthorized: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [error, setError] = useState<unknown>(null),
        [refresh, setRefresh] = useState(0);
    const [reviewed, setReviewed] = useState(false),
        [invalid, setInvalid] = useState(false);
    const callbacks = useRef({ onChange, onUnauthorized });
    callbacks.current = { onChange, onUnauthorized };
    const { allocationId, paymentId, capacity } = draft;
    useEffect(() => {
        if (capacity) return;
        const abort = new AbortController();
        getAllocationReturnCapacity(ledgerId, allocationId, abort.signal)
            .then((value) => {
                if (abort.signal.aborted) return;
                if (
                    value.id !== allocationId ||
                    value.ledger_id !== ledgerId ||
                    value.payment_id !== paymentId ||
                    !Array.isArray(value.lines) ||
                    value.lines.length < 1 ||
                    value.lines.length > 200
                )
                    throw new ApiError("server", 200, "invalid_response");
                for (const row of value.lines) {
                    for (const field of [
                        "payment_amount",
                        "document_amount",
                        "returned_payment_amount",
                        "returned_document_amount",
                        "remaining_payment_amount",
                        "remaining_document_amount",
                    ] as const)
                        if (parseAmount(row[field], 18) < 0n)
                            throw new ApiError("server", 200, "invalid_response");
                    if (typeof row.description !== "string")
                        throw new ApiError("server", 200, "invalid_response");
                }
                setError(null);
                callbacks.current.onChange((current) =>
                    current.allocationId === allocationId
                        ? { ...current, capacity: value }
                        : current,
                );
            })
            .catch((caught) => {
                if (abort.signal.aborted) return;
                if (caught instanceof ApiError && caught.status === 401)
                    callbacks.current.onUnauthorized();
                else setError(caught);
            });
        return () => abort.abort();
    }, [ledgerId, allocationId, paymentId, capacity, refresh]);
    const scale = (asset: string) => assets.find((item) => item.asset_id === asset)?.scale;
    const paymentScale = capacity ? scale(capacity.asset_id) : undefined;
    const computed = new Map<string, string>();
    let body: AllocationReturnCreate | null = null,
        review: AllocationReturnReview | null = null;
    if (capacity && paymentScale !== undefined) {
        try {
            review = { capacity, paymentScale, documentScales: {} };
            for (const line of capacity.lines) {
                const documentScale = scale(line.document_asset_id);
                if (documentScale === undefined) throw new Error("Asset unavailable");
                review.documentScales[line.document_asset_id] = documentScale;
                if (Object.hasOwn(draft.amounts, line.source_allocation_line_id))
                    computed.set(
                        line.source_allocation_line_id,
                        returnPaymentAmount(
                            line,
                            draft.amounts[line.source_allocation_line_id],
                            paymentScale,
                            documentScale,
                        ),
                    );
            }
            body = {
                id: draft.id,
                payment_id: paymentId,
                return_date: draft.date,
                reason: draft.reason,
                lines: capacity.lines
                    .filter((line) => Object.hasOwn(draft.amounts, line.source_allocation_line_id))
                    .map((line) => ({
                        source_allocation_line_id: line.source_allocation_line_id,
                        expected_version: line.expected_version,
                        payment_amount: computed.get(line.source_allocation_line_id)!,
                        document_amount: draft.amounts[line.source_allocation_line_id],
                    })),
            };
            if (!validReturnReview(ledgerId, body, review)) body = null;
        } catch {
            body = null;
        }
    }
    function edit(update: (value: ReturnEditor) => ReturnEditor) {
        setReviewed(false);
        setInvalid(false);
        onChange(update);
    }
    async function submit(event: FormEvent) {
        event.preventDefault();
        if (disabled || !reviewed || !body || !review) {
            setInvalid(true);
            return;
        }
        try {
            setError(null);
            await onSubmit(body, review);
        } catch (caught) {
            if (caught instanceof ApiError && caught.status === 401) onUnauthorized();
            else setError(caught);
        }
    }
    return (
        <form onSubmit={submit} aria-label={t("分摊退回", "Return allocation")}>
            <h3>{t("分摊退回", "Return allocation")}</h3>
            <p>
                {t(
                    "释放原分摊的已应用金额，保留原记录；资金和收入费用不会因此变化。真实退款另行登记。",
                    "Release amounts applied by the original allocation and retain its history. Cash, income and expenses stay unchanged. Record an actual refund separately.",
                )}
            </p>
            {error !== null && <p role="alert">{settlementError(error, locale)}</p>}
            {!capacity && (
                <>
                    <p role="status">
                        {t(
                            "读取原分摊及可退数量…",
                            "Loading original allocation and return capacity…",
                        )}
                    </p>
                    <button type="button" onClick={() => setRefresh((n) => n + 1)}>
                        {t("重新读取", "Retry reading")}
                    </button>
                </>
            )}
            {capacity && (
                <>
                    <p>
                        {t("原分摊日", "Original allocation date")}: {capacity.allocation_date}
                    </p>
                    <label>
                        {t("退回生效日", "Return effective date")}
                        <input
                            type="date"
                            required
                            value={draft.date}
                            min={capacity.allocation_date}
                            disabled={disabled}
                            onChange={(e) => edit((d) => ({ ...d, date: e.target.value }))}
                        />
                    </label>
                    <label>
                        {t("退回原因", "Return reason")}
                        <textarea
                            required
                            maxLength={2000}
                            value={draft.reason}
                            disabled={disabled}
                            onChange={(e) => edit((d) => ({ ...d, reason: e.target.value }))}
                        />
                    </label>
                    <ol className="confirmation-lines">
                        {capacity.lines.map((line) => {
                            const id = line.source_allocation_line_id,
                                selected = Object.hasOwn(draft.amounts, id);
                            const available =
                                line.source_status === "active" &&
                                line.expected_version === 1 &&
                                parseAmount(line.remaining_document_amount, 18) > 0n;
                            return (
                                <li key={id}>
                                    <label>
                                        <input
                                            type="checkbox"
                                            checked={selected}
                                            disabled={disabled || !available}
                                            onChange={(e) => {
                                                const checked = e.target.checked;
                                                edit((d) => {
                                                    const amounts = { ...d.amounts };
                                                    if (checked)
                                                        amounts[id] =
                                                            line.remaining_document_amount;
                                                    else delete amounts[id];
                                                    return { ...d, amounts };
                                                });
                                            }}
                                        />
                                        {line.description || t("单据明细", "Document line")}
                                    </label>
                                    <p>
                                        {t("可退单据金额", "Document amount available")}:{" "}
                                        {formatAmount(line.remaining_document_amount, locale)}{" "}
                                        {line.document_asset_id}
                                    </p>
                                    <p>
                                        {t("可退付款金额", "Payment amount available")}:{" "}
                                        {formatAmount(line.remaining_payment_amount, locale)}{" "}
                                        {capacity.asset_id}
                                    </p>
                                    {selected && (
                                        <>
                                            <label>
                                                {t("本次退回单据金额", "Document amount to return")}
                                                <input
                                                    inputMode="decimal"
                                                    required
                                                    value={draft.amounts[id]}
                                                    disabled={disabled}
                                                    onChange={(e) =>
                                                        edit((d) => ({
                                                            ...d,
                                                            amounts: {
                                                                ...d.amounts,
                                                                [id]: e.target.value,
                                                            },
                                                        }))
                                                    }
                                                />
                                            </label>
                                            <p>
                                                {t(
                                                    "本次退回付款金额（原比例）",
                                                    "Payment amount to return (original ratio)",
                                                )}
                                                :{" "}
                                                {computed.get(id)
                                                    ? `${formatAmount(computed.get(id)!, locale)} ${capacity.asset_id}`
                                                    : "—"}
                                            </p>
                                        </>
                                    )}
                                </li>
                            );
                        })}
                    </ol>
                    <p>
                        {t(
                            "按原分摊比例及付款资产精度计算；末次全额退回吸收剩余差额。过小金额或只剩一侧余量时，请调整数量或整行退回。",
                            "Use the original ratio and payment precision; the final full return absorbs the remainder. Adjust a too-small partial amount or return the full line if only one side would remain.",
                        )}
                    </p>
                    <label>
                        <input
                            type="checkbox"
                            checked={reviewed}
                            disabled={disabled || !body}
                            onChange={(e) => setReviewed(e.target.checked)}
                        />
                        {t(
                            "已核对原来源、日期、原因及两侧数量",
                            "I reviewed the original sources, date, reason and both quantities",
                        )}
                    </label>
                    {invalid && (
                        <p role="alert">
                            {t(
                                "请选择有效源行，检查金额、日期和原因，并确认两侧数量。",
                                "Select valid source lines, check amounts, date and reason, then confirm both quantities.",
                            )}
                        </p>
                    )}
                </>
            )}
            <div className="form-actions">
                <button type="submit" disabled={disabled || !body || !reviewed}>
                    {t("确认退回分摊", "Confirm allocation return")}
                </button>
                <button type="button" onClick={onCancel}>
                    {t("返回收付列表", "Back to settlement list")}
                </button>
            </div>
        </form>
    );
}
