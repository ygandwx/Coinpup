import { useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import type { Account, Asset, Category } from "./ledger-api";
import type { Locale } from "./i18n";
import type { RefundCreate, RefundCurrent, RefundDetail } from "./refund-api";
import { validRefundRevisionReview } from "./refund-revision-intent";
import type { RefundRevisionInput, RefundRevisionReview } from "./refund-revision-intent";
import { BusinessRefundForm } from "./BusinessRefundForm";
import type { RefundEditor } from "./BusinessRefundForm";
import {
    prepareRefundEditor,
    replacementFromEditor,
    validRevisionReason,
} from "./refund-revision-editor";
import { settlementError } from "./settlement-errors";

export type RefundRevisionActions = {
    accounts: Account[];
    categories: Category[];
    disabled: boolean;
    submit: (input: RefundRevisionInput, review: RefundRevisionReview) => Promise<void>;
};
export function BusinessRefundRevisionEditor({
    original,
    current,
    assets,
    locale,
    actions,
    onUnauthorized,
}: {
    original: RefundDetail;
    current: RefundCurrent;
    assets: Asset[];
    locale: Locale;
    actions: RefundRevisionActions;
    onUnauthorized: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [action, setAction] = useState<"correct" | "cancel" | null>(null),
        [reason, setReason] = useState(""),
        [checked, setChecked] = useState(false);
    const [draft, setDraft] = useState<RefundEditor | null>(null),
        [error, setError] = useState<unknown>(null),
        [refresh, setRefresh] = useState(0);
    const callback = useRef(onUnauthorized);
    callback.current = onUnauthorized;
    useEffect(() => {
        if (action !== "correct") return;
        const abort = new AbortController();
        prepareRefundEditor(original, current, abort.signal).then(
            (value) => {
                if (!abort.signal.aborted) setDraft(value);
            },
            (caught) => {
                if (abort.signal.aborted) return;
                setError(caught);
                if (caught instanceof ApiError && caught.status === 401) callback.current();
            },
        );
        return () => abort.abort();
    }, [action, original, current, refresh]);
    if (current.status !== "active" || !current.fact) return null;
    const facts = { original, current, assets };
    function select(next: typeof action) {
        setChecked(false);
        setReason("");
        setDraft(null);
        setError(null);
        setAction(next);
    }
    const correction = (body: RefundCreate): RefundRevisionInput => ({
        action: "correct",
        refundId: current.refund_id,
        body: {
            expected_version: current.version,
            reason,
            replacement: replacementFromEditor(body),
        },
    });
    async function send(input: RefundRevisionInput, review: RefundRevisionReview) {
        if (actions.disabled || !validRefundRevisionReview(current.ledger_id, input, review)) {
            setError(new ApiError("server", 422, "invalid_settlement_intent"));
            return;
        }
        try {
            await actions.submit(input, review);
        } catch (caught) {
            setError(caught);
            if (caught instanceof ApiError && caught.status === 401) callback.current();
        }
    }
    if (!action)
        return (
            <div className="form-actions">
                <button type="button" disabled={actions.disabled} onClick={() => select("correct")}>
                    {t("更正此笔退款", "Correct this refund")}
                </button>
                <button type="button" disabled={actions.disabled} onClick={() => select("cancel")}>
                    {t("取消此笔退款", "Cancel this refund")}
                </button>
            </div>
        );
    return (
        <section
            aria-label={
                action === "correct"
                    ? t("更正此笔退款", "Correct this refund")
                    : t("取消此笔退款", "Cancel this refund")
            }
        >
            {error != null && <p role="alert">{settlementError(error, locale)}</p>}
            {action === "correct" ? (
                draft ? (
                    <BusinessRefundForm
                        ledgerId={current.ledger_id}
                        locale={locale}
                        accounts={actions.accounts}
                        assets={assets}
                        categories={actions.categories}
                        disabled={actions.disabled}
                        draft={draft}
                        onChange={(fn) => setDraft((previous) => (previous ? fn(previous) : null))}
                        onUnauthorized={onUnauthorized}
                        revision={{
                            reason,
                            setReason,
                            ownPrincipal: current.fact.payment_amount,
                            valid: (body, cash) =>
                                validRefundRevisionReview(current.ledger_id, correction(body), {
                                    ...facts,
                                    cash,
                                }),
                        }}
                        onSubmit={(body, cash) => send(correction(body), { ...facts, cash })}
                        onCancel={() => select(null)}
                    />
                ) : (
                    <div>
                        {error == null ? (
                            <p role="status">
                                {t(
                                    "读取付款和当前退回证据…",
                                    "Loading payment and current return evidence…",
                                )}
                            </p>
                        ) : (
                            <button
                                type="button"
                                onClick={() => {
                                    setError(null);
                                    setRefresh((x) => x + 1);
                                }}
                            >
                                {t("重新读取来源", "Reload sources")}
                            </button>
                        )}
                        <button type="button" onClick={() => select(null)}>
                            {t("返回当前版本", "Back to current version")}
                        </button>
                    </div>
                )
            ) : (
                <form
                    className="refund-form"
                    onSubmit={(e) => {
                        e.preventDefault();
                        if (checked && validRevisionReason(reason))
                            void send(
                                {
                                    action: "cancel",
                                    refundId: current.refund_id,
                                    body: { expected_version: current.version, reason },
                                },
                                facts,
                            );
                    }}
                >
                    <label className="field">
                        {t("取消原因", "Cancellation reason")}
                        <textarea
                            required
                            value={reason}
                            onChange={(e) => {
                                setReason(e.target.value);
                                setChecked(false);
                            }}
                        />
                    </label>
                    <label className="archive-toggle">
                        <input
                            type="checkbox"
                            checked={checked}
                            onChange={(e) => setChecked(e.currentTarget.checked)}
                        />
                        {t(
                            "我已核对当前退款，确认冲销现金和独立手续费，保留原始回执。",
                            "I reviewed this refund and confirm the reversal of cash and separate fees. The original receipt is retained.",
                        )}
                    </label>
                    <div className="form-actions">
                        <button
                            type="submit"
                            disabled={actions.disabled || !checked || !validRevisionReason(reason)}
                        >
                            {t("确认取消退款", "Confirm refund cancellation")}
                        </button>
                        <button type="button" onClick={() => select(null)}>
                            {t("保留当前退款", "Keep current refund")}
                        </button>
                    </div>
                </form>
            )}
        </section>
    );
}
