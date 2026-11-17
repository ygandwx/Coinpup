import { useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import type { AllocationReturnDetail, AllocationReturnCapacity } from "./allocation-return-api";
import type { ReturnCurrent, ReturnCorrection } from "./allocation-return-revision-api";
import type {
    ReturnRevisionInput,
    ReturnRevisionReview,
} from "./allocation-return-revision-intent";
import { reviewedSources, validReturnRevisionReview } from "./allocation-return-revision-intent";
import { loadReturnSourcePage } from "./return-revision-sources";
import type { Asset } from "./ledger-api";
import type { Locale } from "./i18n";
import { amountFromMinorUnits } from "./money";
import { storedUnits } from "./settlement-revision-facts";
import { settlementError } from "./settlement-errors";

export type ReturnRevisionActions = {
    disabled: boolean;
    submit: (input: ReturnRevisionInput, review: ReturnRevisionReview) => Promise<void>;
};
type Props = {
    original: AllocationReturnDetail;
    current: ReturnCurrent;
    assets: Asset[];
    locale: Locale;
    actions: ReturnRevisionActions;
    onUnauthorized: () => void;
};
type Line = ReturnCorrection["replacement"]["lines"][number];
export function BusinessReturnRevisionEditor(props: Props) {
    const [action, setAction] = useState<"correct" | "cancel" | null>(null);
    const t = (zh: string, en: string) => (props.locale === "zh" ? zh : en);
    if (props.current.status !== "active" || !props.current.fact) return null;
    if (!action)
        return (
            <div className="form-actions">
                <button
                    type="button"
                    disabled={props.actions.disabled}
                    onClick={() => setAction("correct")}
                >
                    {t("更正此组退回", "Correct this return group")}
                </button>
                <button
                    type="button"
                    disabled={props.actions.disabled}
                    onClick={() => setAction("cancel")}
                >
                    {t("取消此组退回", "Cancel this return group")}
                </button>
            </div>
        );
    return <ReturnRevisionForm {...props} action={action} onExit={() => setAction(null)} />;
}

function ReturnRevisionForm({
    original,
    current,
    assets,
    locale,
    actions,
    onUnauthorized,
    action,
    onExit,
}: Props & { action: "correct" | "cancel"; onExit: () => void }) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [reason, setReason] = useState(""),
        [factReason, setFactReason] = useState(current.fact!.reason),
        [day, setDay] = useState(current.fact!.return_date);
    const [lines, setLines] = useState<Line[]>(() => {
        const exact = (value: string, id: string) =>
            amountFromMinorUnits(
                storedUnits(value, id, assets),
                assets.find((a) => a.asset_id === id)!.scale,
            );
        return current.members.map((m) => ({
            source_allocation_line_id: m.source_allocation_line_id,
            expected_version: 1,
            payment_amount: exact(m.payment_amount, current.payment_asset_id),
            document_amount: exact(m.document_amount, m.document_asset_id),
        }));
    });
    const [capacities, setCapacities] = useState<AllocationReturnCapacity[]>([]),
        [selected, setSelected] = useState("");
    const [page, setPage] = useState(0),
        [reload, setReload] = useState(0),
        [completed, setCompleted] = useState(-1),
        [more, setMore] = useState(false);
    const [reviewed, setReviewed] = useState(false),
        [error, setError] = useState<unknown>(null);
    const loaded = useRef<AllocationReturnCapacity[]>([]);
    const callback = useRef(onUnauthorized);
    callback.current = onUnauthorized;
    useEffect(() => {
        if (action !== "correct") return;
        const abort = new AbortController();
        loadReturnSourcePage(original, current, page, abort.signal)
            .then((result) => {
                if (abort.signal.aborted) return;
                const groups = new Map(loaded.current.map((c) => [c.id, c]));
                for (const c of result.capacities) groups.set(c.id, c);
                const next = [...groups.values()];
                reviewedSources(next);
                loaded.current = next;
                setCapacities(next);
                setMore(result.more);
                setCompleted(page);
                setError(null);
            })
            .catch((cause) => {
                if (abort.signal.aborted) return;
                if (cause instanceof ApiError && cause.status === 401) callback.current();
                else setError(cause);
            });
        return () => abort.abort();
    }, [action, original, current, page, reload]);
    const sources = reviewedSources(capacities);
    const selectedIds = new Set(lines.map((l) => l.source_allocation_line_id));
    const input: ReturnRevisionInput =
        action === "correct"
            ? {
                  action,
                  returnId: original.id,
                  body: {
                      expected_version: current.version,
                      reason,
                      replacement: { return_date: day, reason: factReason, lines },
                  },
              }
            : {
                  action,
                  returnId: original.id,
                  body: { expected_version: current.version, reason },
              };
    const facts: ReturnRevisionReview = {
        original,
        current,
        assets,
        capacities:
            action === "correct"
                ? capacities.filter((c) =>
                      c.lines.some((l) => selectedIds.has(l.source_allocation_line_id)),
                  )
                : [],
    };
    const ready =
        !actions.disabled &&
        (action === "cancel" || page === completed) &&
        validReturnRevisionReview(current.ledger_id, input, facts);
    const change = (fn: () => void) => {
        fn();
        setReviewed(false);
    };
    const lineChange = (
        index: number,
        field: "payment_amount" | "document_amount",
        value: string,
    ) =>
        change(() =>
            setLines((rows) =>
                rows.map((row, i) => (i === index ? { ...row, [field]: value } : row)),
            ),
        );
    function move(index: number, delta: number) {
        change(() =>
            setLines((rows) => {
                const next = [...rows];
                [next[index], next[index + delta]] = [next[index + delta], next[index]];
                return next;
            }),
        );
    }
    async function submit() {
        if (!ready || !reviewed) return;
        try {
            await actions.submit(input, facts);
        } catch (cause) {
            if (cause instanceof ApiError && cause.status === 401) callback.current();
            else setError(cause);
        }
    }
    return (
        <form
            className="allocation-return-form revision-form"
            aria-label={
                action === "correct"
                    ? t("退回更正", "Return correction")
                    : t("退回取消", "Return cancellation")
            }
            onSubmit={(event) => {
                event.preventDefault();
                void submit();
            }}
        >
            <h4>
                {action === "correct"
                    ? t("更正退回组", "Correct return group")
                    : t("取消退回组", "Cancel return group")}{" "}
                · {t("原版本", "Expected version")} {current.version}
            </h4>
            {action === "cancel" && (
                <p>
                    {t(
                        "将冲销以上全部当前成员。仍有有效退款引用时，先显式更正退款证据。",
                        "All current members above will be reversed. If an active refund references this return, explicitly correct its evidence first.",
                    )}
                </p>
            )}
            <label className="field">
                {t("修订原因", "Revision reason")}
                <textarea
                    value={reason}
                    onChange={(e) => change(() => setReason(e.target.value))}
                    required
                />
            </label>
            {action === "correct" && (
                <>
                    <label className="field">
                        {t("替代退回日期", "Replacement return date")}
                        <input
                            type="date"
                            value={day}
                            onChange={(e) => change(() => setDay(e.target.value))}
                            required
                        />
                    </label>
                    <label className="field">
                        {t("替代退回原因", "Replacement return reason")}
                        <textarea
                            value={factReason}
                            onChange={(e) => change(() => setFactReason(e.target.value))}
                            required
                        />
                    </label>
                    <p>
                        {t(
                            "保留来源可重新使用本组此前退回的数量。付款侧和单据侧须同时核对，服务端将验证原序位、后续舍入和实时可用量。",
                            "Retained sources can reuse this group’s previous amounts. Review both quantities. The server verifies the original position, later rounding and current capacity.",
                        )}
                    </p>
                    <ol className="confirmation-lines">
                        {lines.map((line, i) => {
                            const id = line.source_allocation_line_id,
                                item = sources.get(id)?.source,
                                old = current.members.find(
                                    (m) => m.source_allocation_line_id === id,
                                );
                            return (
                                <li key={id}>
                                    <strong>
                                        {item?.description || t("分摊来源", "Allocation source")}
                                    </strong>
                                    <p>
                                        {id}
                                        {old && (
                                            <>
                                                {" "}
                                                · {t("来源序位", "Source position")}{" "}
                                                {old.source_return_no}
                                            </>
                                        )}
                                    </p>
                                    {item && (
                                        <p>
                                            {t("当前剩余", "Currently available")}{" "}
                                            {item.remaining_payment_amount}{" "}
                                            {current.payment_asset_id} /{" "}
                                            {item.remaining_document_amount}{" "}
                                            {item.document_asset_id}
                                        </p>
                                    )}
                                    <label className="field">
                                        {t("付款侧数量", "Payment quantity")} ·{" "}
                                        {current.payment_asset_id}
                                        <input
                                            inputMode="decimal"
                                            value={line.payment_amount}
                                            onChange={(e) =>
                                                lineChange(i, "payment_amount", e.target.value)
                                            }
                                            required
                                        />
                                    </label>
                                    <label className="field">
                                        {t("单据侧数量", "Document quantity")} ·{" "}
                                        {item?.document_asset_id ?? old?.document_asset_id}
                                        <input
                                            inputMode="decimal"
                                            value={line.document_amount}
                                            onChange={(e) =>
                                                lineChange(i, "document_amount", e.target.value)
                                            }
                                            required
                                        />
                                    </label>
                                    <div className="form-actions">
                                        <button
                                            type="button"
                                            disabled={i === 0}
                                            onClick={() => move(i, -1)}
                                        >
                                            {t("上移", "Move up")}
                                        </button>
                                        <button
                                            type="button"
                                            disabled={i + 1 === lines.length}
                                            onClick={() => move(i, 1)}
                                        >
                                            {t("下移", "Move down")}
                                        </button>
                                        <button
                                            type="button"
                                            onClick={() =>
                                                change(() =>
                                                    setLines((rows) =>
                                                        rows.filter(
                                                            (l) =>
                                                                l.source_allocation_line_id !== id,
                                                        ),
                                                    ),
                                                )
                                            }
                                        >
                                            {t("移除来源", "Remove source")}
                                        </button>
                                    </div>
                                </li>
                            );
                        })}
                    </ol>
                    <label className="field">
                        {t("可添加的来源", "Source to add")}
                        <select value={selected} onChange={(e) => setSelected(e.target.value)}>
                            <option value="">{t("选择来源", "Select source")}</option>
                            {[...sources.values()]
                                .filter(
                                    ({ source: s }) =>
                                        s.source_status === "active" &&
                                        !selectedIds.has(s.source_allocation_line_id),
                                )
                                .map(({ source: s }) => (
                                    <option
                                        key={s.source_allocation_line_id}
                                        value={s.source_allocation_line_id}
                                    >
                                        {s.description} · {s.document_asset_id} ·{" "}
                                        {s.source_allocation_line_id}
                                    </option>
                                ))}
                        </select>
                    </label>
                    <div className="form-actions">
                        <button
                            type="button"
                            disabled={!selected || selectedIds.has(selected) || lines.length >= 200}
                            onClick={() =>
                                change(() => {
                                    setLines((rows) => [
                                        ...rows,
                                        {
                                            source_allocation_line_id: selected,
                                            expected_version: 1,
                                            payment_amount: "",
                                            document_amount: "",
                                        },
                                    ]);
                                    setSelected("");
                                })
                            }
                        >
                            {t("添加来源", "Add source")}
                        </button>
                        {more && (
                            <button
                                type="button"
                                disabled={completed !== page}
                                onClick={() => change(() => setPage((p) => p + 25))}
                            >
                                {t("读取更多分摊来源", "Load more allocation sources")}
                            </button>
                        )}
                        {error != null && completed !== page && (
                            <button
                                type="button"
                                onClick={() =>
                                    change(() => {
                                        setError(null);
                                        setReload((n) => n + 1);
                                    })
                                }
                            >
                                {t("重读来源", "Retry source lookup")}
                            </button>
                        )}
                    </div>
                    {completed !== page && error == null && (
                        <p role="status">{t("正在读取分摊来源…", "Loading allocation sources…")}</p>
                    )}
                    {more && lines.some((l) => !sources.has(l.source_allocation_line_id)) && (
                        <p>
                            {t(
                                "部分当前成员属于其他分摊页，请读取更多来源后核对。",
                                "Some current members belong to older allocations. Load more sources to review them.",
                            )}
                        </p>
                    )}
                </>
            )}
            {action === "correct" &&
                completed === page &&
                !actions.disabled &&
                !ready &&
                reason.trim() &&
                factReason.trim() && (
                    <p role="status">
                        {t(
                            "核对所选来源、退回日期和两侧金额。金额须为正，符合资产精度，并在剩余量加本组原数量以内。",
                            "Check the sources, return date and both amounts. Each amount must be positive, fit its asset precision and stay within available capacity plus this group's previous amount.",
                        )}
                    </p>
                )}
            {error != null && <p role="alert">{settlementError(error, locale)}</p>}
            <label className="archive-toggle">
                <input
                    type="checkbox"
                    checked={reviewed}
                    disabled={!ready}
                    onChange={(e) => setReviewed(e.target.checked)}
                />
                {t(
                    "我已核对整组来源、两侧数量及修订原因",
                    "I reviewed every source, both quantities and the revision reason",
                )}
            </label>
            <div className="form-actions">
                <button type="submit" className="primary-button" disabled={!ready || !reviewed}>
                    {action === "correct"
                        ? t("提交退回更正", "Submit return correction")
                        : t("提交退回取消", "Submit return cancellation")}
                </button>
                <button type="button" onClick={onExit}>
                    {t("放弃本次编辑", "Discard these edits")}
                </button>
            </div>
        </form>
    );
}
